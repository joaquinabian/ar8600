"""Bounded, asynchronous preparation/restoration of a single-bank linked scan.

No channel recalls or memory writes: MA proves the helper empty and GM/BM
read-back identifies and verifies every group we temporarily change.
"""
import re

from aor_functions import parse_group_members, parse_operation_parameter, operation_parameter_command

BANK_ORDER = 'AaBbCcDdEeFfGgHhIiJj'
PARAMETERS = ('XA', 'XB', 'XD', 'XM', 'XP')
PARAMETER_PATTERN = r'X[AB][ +]?[0-9]{3}|XD(?:[0-9]\.[0-9]|[0-9]{2})|XM[0-8F]|XP[0-9]{2}'


def group_snapshot(header, membership):
    match = re.match(r'GM([0-9])(?:\s|$)', header)
    if not match:
        raise ValueError('Invalid GM response')
    raw = {}
    values = {}
    fields = list(re.finditer(PARAMETER_PATTERN, header[match.end():]))
    if re.sub(PARAMETER_PATTERN, '', header[match.end():]).strip():
        raise ValueError('Unrecognized/incomplete GM parameter data')
    for item in fields:
        field = item.group()
        key, value = parse_operation_parameter(field)
        if key in raw:
            raise ValueError('Duplicate group parameter')
        # Read-back XD2.0 must be restored with the documented setter XD20.
        # XP can legitimately exceed the dialog's 60 s limit: retain it exactly.
        raw[key] = ('XP%02d' % value if key == 'XP' else
                    operation_parameter_command(key, value).decode('ascii').rstrip('\r\n'))
        values[key] = value
    if set(raw) != set(PARAMETERS):
        raise ValueError('Incomplete GM settings; XA/XB/XD/XM/XP are required')
    parse_group_members(membership, 'scan')
    # Preserve the scanner's returned membership order, rather than the GUI order.
    members = tuple(membership[2:].strip().replace('-', ''))
    return {'group': int(match[1]), 'members': members, 'raw': raw, 'values': values}


class CurrentBankScan:
    def __init__(self, controller, target, level=None, ready=None):
        self.controller = controller
        self.target = target['bank']
        self.level = level
        self.ready_callback = ready
        self.phase = 'empty'
        self.timer = None
        self.sending = False
        self.group = None
        self.saved = None
        self.original = None
        self.modified = False
        self.header = None
        self.expected_group = None
        self.probes = {}
        self.finish_callback = None
        self.failure = None
        state = controller.receiver_status or {}
        self.receiver_command = (('MR' + state['channel'] + '\r\n').encode('ascii')
                                 if state.get('context') == 'MR' and state.get('channel') else
                                 controller.normal_vfo_command())
        self.candidates = [bank for bank in BANK_ORDER if bank != self.target and bank in controller.memory_banks]

    @property
    def busy(self):
        return self.phase not in ('ready', 'active', 'finished')

    def arm(self, milliseconds=5000):
        if self.timer is not None:
            self.timer.Stop()
        self.timer = self.controller.scan_call_later(milliseconds, self.timeout)

    def send(self, command):
        self.arm(10000 if self.phase == 'empty' else 5000)
        self.sending = True
        try:
            ok = self.controller.write_serial(command) == len(command)
        finally:
            self.sending = False
        if not ok:
            self.fail('Serial write failed during ' + self.phase)
        return ok

    def start(self):
        if len(self.controller.memory_banks) != 20:
            self.fail('Complete TB metadata is required before starting Current Memory Bank.')
            return
        self.next_empty_bank()

    def next_empty_bank(self):
        if not self.candidates:
            self.fail('No genuinely empty helper Memory Bank was verified; scan not started, groups unchanged.')
            return
        self.helper = self.candidates.pop(0)
        capacity = self.controller.memory_banks[self.helper]['channels']
        # Even a zero-capacity bank must answer MA with explicit empty slots.
        blocks = max(1, (capacity + 9) // 10)
        self.remaining = {'%s%02d' % (self.helper, n) for n in range(blocks * 10)}
        self.populated = False
        self.phase = 'empty'
        self.controller.aor_status.SetStatusText('Verifying empty helper bank %s...' % self.helper)
        self.send(('MA%s\r\n' % self.helper + 'MA\r\n' * (blocks - 1)).encode('ascii'))

    def query_group(self, group, phase):
        self.phase, self.header, self.expected_group = phase, None, group
        self.send((('GM%d\r\n' % group if group is not None else '') + 'GM\r\n').encode('ascii'))

    def receive(self, text):
        if self.phase == 'finished':
            return False
        if text == '?':
            if self.phase == 'empty':
                self.fail('Helper bank %s returned ?; emptiness cannot be verified.' % self.helper)
            elif self.busy or self.controller.level_squelch_pending:
                self.fail('Scanner rejected a temporary scan-group operation during ' + self.phase)
            return True
        try:
            if self.phase == 'starting' and text.startswith('MS '):
                fields = text.split(None, 8)[1:]
                self.controller.validate_fields(fields, ('MX', 'MP', 'RF', 'ST', 'AU', 'MD', 'AT', 'TM'))
                if fields[0][2] != self.target:
                    raise ValueError('Scan started in a different Memory Bank')
                self.phase = 'active'
                self.timer.Stop()
                self.controller.update_connection_ui()
                return False  # Normal validated RX parser owns the displayed scan state.
            if self.phase == 'empty' and text.startswith('MX'):
                channel, fields = self.controller.parse_memory_line(text)
                if channel in self.remaining:
                    self.remaining.remove(channel)
                    self.populated |= fields is not None
                    if not self.remaining:
                        if self.populated:
                            self.next_empty_bank()
                        else:
                            self.query_group(None, 'original')
                return True
            if self.busy and text.startswith('GM'):
                if not re.match(r'GM[0-9](?:\s|$)', text):
                    raise ValueError('Invalid group header')
                if self.expected_group is not None and int(text[2]) != self.expected_group:
                    raise ValueError('Scanner returned the wrong Scan Group')
                self.header = text
                return True
            if self.busy and text.startswith('BM'):
                if self.header is None:
                    if self.phase in ('configured', 'restoring'):
                        # BM replacement may itself reply before GM's complete
                        # verification report. It cannot verify the parameters.
                        parse_group_members(text, 'scan')
                        return True
                    raise ValueError('Membership without confirmed GM header')
                state = group_snapshot(self.header, text)
                self.accept_group(state)
                return True
            if self.busy and text.startswith(PARAMETERS):
                # A setter echo is not a full GM read-back and must not be
                # associated with the previous selected group's cached XB.
                parse_operation_parameter(text)
                return True
        except (ValueError, IndexError, TypeError) as error:
            self.fail('Invalid temporary scan read-back: %s' % error)
            return True
        return False

    def accept_group(self, state):
        if self.phase == 'original':
            self.original = state
            self.query_group(1, 'probe')
        elif self.phase == 'probe':
            self.probes[state['group']] = state
            if not state['members']:
                self.configure(state)
            elif state['group'] < 9:
                self.query_group(state['group'] + 1, 'probe')
            else:
                self.configure(self.probes[1])
        elif self.phase == 'configured':
            expected = dict(self.saved['values'], XB=self.level)
            if set(state['members']) != {self.helper, self.target} or state['values'] != expected:
                raise ValueError('Temporary group membership/settings did not verify')
            self.controller.scan_levels[self.group] = state['values']['XB']
            self.controller.current_scan_level = state['values']['XB']
            self.controller.group_response['scan'] = self.group
            self.phase = 'ready'
            self.timer.Stop()
            self.controller.update_connection_ui()
            if self.ready_callback is not None:
                self.ready_callback()
            else:
                self.begin_scan()
        elif self.phase == 'restoring':
            if state['values'] != self.saved['values'] or state['members'] != self.saved['members']:
                raise ValueError('Original temporary Scan Group state did not verify')
            self.modified = False
            self.controller.scan_levels[self.group] = state['values']['XB']
            self.query_group(self.original['group'], 'restore_context')
        elif self.phase == 'restore_context':
            if state['values'] != self.original['values'] or state['members'] != self.original['members']:
                raise ValueError('Original selected Scan Group state did not verify')
            self.controller.group_response['scan'] = self.original['group']
            self.controller.cbx_scan.SetStringSelection(str(self.original['group']))
            self.controller.scan_levels[self.original['group']] = state['values']['XB']
            self.send(b'RX\r\n')
            if self.phase != 'restore_failed':
                self.finish(self.failure + ' Original Scan Group restored and verified.' if self.failure else
                            'Current Memory Bank stopped; original Scan Group restored and verified.')

    def configure(self, state):
        self.group, self.saved = state['group'], state
        if self.level is None:
            self.level = state['values']['XB']
        # Mark dirty BEFORE writing: even a short/failed write may have changed BM.
        self.modified = True
        self.phase, self.expected_group, self.header = 'configured', self.group, None
        command = ('GM%d\r\nBM%%%%%s%s\r\n' % (self.group, self.helper, self.target)).encode('ascii')
        command += operation_parameter_command('XB', self.level) + b'GM\r\n'
        self.send(command)

    def begin_scan(self):
        if self.phase != 'ready':
            return
        self.phase = 'starting'
        command = ('GM%d\r\nMS%s\r\nRX\r\n' % (self.group, self.target)).encode('ascii')
        self.controller.active_operation = ('Memory Scan', 'Current Bank')
        self.controller.operation_signal_open = False
        if self.send(command):
            self.controller.show_now_receiving()
            self.controller.update_connection_ui()
            self.controller.aor_status.SetStatusText('Scanning Memory Bank %s (temporary linked group %d).' % (self.target, self.group))

    def restore(self, callback=None):
        if callback is not None:
            self.finish_callback = callback
        if self.phase in ('restoring', 'restore_context'):
            return
        if self.timer is not None:
            self.timer.Stop()
        dialog = getattr(self.controller, 'settings_dialog', None)
        if dialog is not None and dialog.family == 'X' and dialog.group == self.group:
            dialog.Close()
        self.controller.active_operation = None
        self.controller.operation_signal_open = False
        if self.original is None:
            self.finish(self.failure or 'Current Memory Bank preparation cancelled; groups unchanged.')
            return
        if not self.controller.serial.is_open:
            self.restoration_failed('Serial port unavailable; original group could not be restored.')
            return
        self.header = None
        command = self.receiver_command
        if self.modified:
            self.phase, self.expected_group = 'restoring', self.group
            command += ('GM%d\r\nBM%%%%%s\r\n' % (self.group, ''.join(self.saved['members']))).encode('ascii')
            # Preserve ALL returned settings, including valid values outside the GUI range.
            command += ''.join(self.saved['raw'][key] + '\r\n' for key in PARAMETERS).encode('ascii')
            command += b'GM\r\n'
        else:
            self.phase, self.expected_group = 'restore_context', self.original['group']
            command += ('GM%d\r\nGM\r\n' % self.original['group']).encode('ascii')
        self.send(command)
        self.controller.update_connection_ui()

    def fail(self, message):
        self.failure = message
        if self.phase in ('restoring', 'restore_context', 'restore_failed'):
            self.restoration_failed(message)
        else:
            self.restore()

    def restoration_failed(self, message):
        if self.timer is not None:
            self.timer.Stop()
        self.phase = 'restore_failed'
        self.controller.report_scan_error(message + ' Original state retained; Stop can attempt restoration again.')
        self.controller.update_connection_ui()
        callback, self.finish_callback = self.finish_callback, None
        if callback is not None:
            callback()

    def timeout(self):
        self.fail('Temporary scan-group %s read-back timed out.' % self.phase)

    def finish(self, message):
        if self.timer is not None:
            self.timer.Stop()
        self.phase = 'finished'
        self.controller.temporary_scan = None
        self.controller.level_squelch_pending = False
        self.controller.refresh_level_context(query=False)
        self.controller.show_now_receiving()
        self.controller.update_connection_ui()
        if self.failure:
            self.controller.report_scan_error(message)
        else:
            self.controller.aor_status.SetStatusText(message)
        callback, self.finish_callback = self.finish_callback, None
        if callback is not None:
            callback()
