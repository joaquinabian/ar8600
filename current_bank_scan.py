"""Bounded, asynchronous preparation/restoration of a single-bank linked scan.

No channel recalls or memory writes: GM/BM read-back identifies and verifies
every group we temporarily change; the group contains only the target bank.
"""
import re
from collections import deque

from aor_functions import (parse_group_members, parse_operation_parameter,
                           operation_parameter_command, group_members_replacement)

COMMAND_PACE_MS = 200
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
        # Retain all documented XP00..XP99 values exactly for restoration.
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
        self.level = getattr(controller, 'level_squelch_value', None) if level is None else level
        profiles = getattr(controller, 'memory_bank_scan_profiles', {})
        self.profile = dict(XA=0, XB=None, XD=20, XM='F',
                            XP=getattr(controller, 'memory_bank_dwell', {}).get(self.target, 0))
        self.profile.update(profiles.get(self.target, {}))
        if self.profile['XB'] is not None:
            self.level = self.profile['XB']
        self.ready_callback = ready
        self.phase = 'initial'
        self.timer = None
        self.pace_timer = None
        self.command_queue = deque()
        self.current_command = None
        self.sequence_phase = None
        self.sequence_generation = 0
        self.awaiting_final_group = False
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

    @property
    def busy(self):
        return self.phase not in ('ready', 'active', 'finished')

    def arm(self, milliseconds=5000):
        if self.timer is not None:
            self.timer.Stop()
        self.timer = self.controller.scan_call_later(milliseconds, self.timeout)

    def send(self, command):
        self.arm()
        self.sending = True
        try:
            ok = self.controller.write_serial(command) == len(command)
        finally:
            self.sending = False
        if not ok:
            self.fail('Serial write failed for %s.' % self.command_diagnostic(command) if self.current_command is not None
                      else 'Serial write failed during ' + self.phase)
        return ok

    def cancel_sequence(self):
        self.sequence_generation += 1
        if self.pace_timer is not None:
            self.pace_timer.Stop()
        self.pace_timer = None
        self.command_queue.clear()
        self.current_command = None
        self.sequence_phase = None
        self.awaiting_final_group = False

    def send_sequence(self, commands):
        self.cancel_sequence()
        self.command_queue.extend(commands)
        self.sequence_phase = self.phase
        self.send_next_command(self.sequence_generation)

    def send_next_command(self, generation):
        if generation != self.sequence_generation or self.phase != self.sequence_phase:
            return
        self.pace_timer = None
        self.current_command = self.command_queue.popleft()
        self.awaiting_final_group = (not self.command_queue and
                                     self.current_command == getattr(self, 'verification_command', b'GM\r\n'))
        if self.awaiting_final_group:
            self.header = None  # Earlier group-selection echoes cannot verify the sequence.
        if not self.send(self.current_command) or generation != self.sequence_generation:
            return
        if self.command_queue:
            self.pace_timer = self.controller.scan_call_later(
                COMMAND_PACE_MS, lambda: self.send_next_command(generation))

    def command_diagnostic(self, command=None):
        action = 'configuring' if self.phase == 'configured' else 'restoring'
        group = self.original['group'] if self.phase == 'restore_context' else self.group
        return '%s while %s %s Scan Group %d' % (
            (command or self.current_command).decode('ascii').rstrip('\r\n'), action,
            'original' if self.phase == 'restore_context' else 'temporary', group)

    def start(self):
        if not isinstance(self.level, int) or not 0 <= self.level <= 255:
            self.fail('Read the confirmed listening Squelch (DB) before preparing a bank scan.')
            return
        if len(self.controller.memory_banks) != 20:
            self.fail('Complete TB metadata is required before starting Current Memory Bank.')
            return
        self.query_group(None, 'original')

    def query_group(self, group, phase):
        self.phase, self.header, self.expected_group = phase, None, group
        self.controller.aor_status.SetStatusText(
            '%s Memory Bank %s scan: reading Scan Group %s...' %
            ('Restoring' if phase == 'restore_context' else 'Preparing',
             self.target, 'current' if group is None else group))
        if phase == 'restore_context':
            self.send_sequence([('GM%d\r\n' % group).encode('ascii'), b'GM\r\n'])
        else:
            self.send((('GM%d\r\n' % group if group is not None else '') + 'GM\r\n').encode('ascii'))

    def receive(self, text):
        if self.phase == 'finished':
            return False
        if text == '?':
            if self.busy or self.controller.level_squelch_pending:
                self.fail('Scanner rejected %s.' % self.command_diagnostic() if self.current_command is not None
                          else 'Scanner rejected a temporary scan-group operation during ' + self.phase)
            return True
        try:
            if (self.sequence_phase == self.phase and not self.awaiting_final_group and
                    text.startswith(('GM', 'BM'))):
                # Setter/selection echoes may arrive while remaining commands are paced.
                # Only the final GM report can complete configuration/restoration.
                if text.startswith('BM'):
                    parse_group_members(text, 'scan')
                return True
            if self.phase == 'starting' and text.startswith('MS '):
                fields = text.split(None, 8)[1:]
                self.controller.validate_fields(fields, ('MX', 'MP', 'RF', 'ST', 'AU', 'MD', 'AT', 'TM'))
                if fields[0][2] != self.target:
                    raise ValueError('Scan started in a different Memory Bank')
                self.phase = 'active'
                self.timer.Stop()
                self.controller.update_connection_ui()
                self.controller.aor_status.SetStatusText('Scanning Memory Bank %s' % self.target)
                return False  # Normal validated RX parser owns the displayed scan state.
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
            expected = dict(self.profile, XB=self.level)
            if tuple(state['members']) != (self.target,) or state['values'] != expected:
                raise ValueError('Temporary group membership/settings did not verify')
            self.cancel_sequence()
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
        elif self.phase == 'level_start':
            if tuple(state['members']) != (self.target,) or state['values']['XB'] != self.level:
                raise ValueError('Listening Squelch did not verify as temporary Scan Level')
            self.controller.scan_levels[self.group] = self.level
            self.controller.current_scan_level = self.level
            self.controller.group_response['scan'] = self.group
            self.phase = 'ready'
            self.timer.Stop()
            self.controller.update_connection_ui()
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
                self.failure = None
                self.finish('Current Memory Bank stopped; original Scan Group restored and verified.')

    def configure(self, state):
        self.group, self.saved = state['group'], state
        # Mark dirty BEFORE writing: even a short/failed write may have changed BM.
        self.modified = True
        self.phase, self.expected_group, self.header = 'configured', self.group, None
        self.controller.aor_status.SetStatusText(
            'Preparing Memory Bank %s scan: configuring temporary Scan Group %d with Squelch %d...' %
            (self.target, self.group, self.level))
        commands = [('GM%d\r\n' % self.group).encode('ascii'),
                    group_members_replacement('scan', self.target)]
        values = dict(self.profile, XB=self.level)
        commands.extend(operation_parameter_command(key, values[key]) for key in PARAMETERS)
        commands.append(b'GM\r\n')
        self.send_sequence(commands)

    def begin_scan(self, level=None):
        profile = getattr(self.controller, 'memory_bank_scan_profiles', {}).get(self.target, self.profile)
        if profile.get('XB') is not None:
            level = profile['XB']
        if self.phase != 'ready':
            return
        if level is not None and self.controller.scan_levels.get(self.group) != level:
            self.level = level
            self.phase, self.header, self.expected_group = 'level_start', None, self.group
            self.controller.aor_status.SetStatusText(
                'Preparing Memory Bank %s scan: verifying listening Squelch %d...' % (self.target, level))
            self.send(operation_parameter_command('XB', level) + b'GM\r\n')
            self.controller.update_connection_ui()
            return
        self.phase = 'starting'
        command = ('GM%d\r\nMS%s\r\nRX\r\n' % (self.group, self.target)).encode('ascii')
        self.controller.active_operation = ('Memory Scan', 'Current Bank')
        self.controller.operation_signal_open = False
        if self.send(command):
            self.controller.show_now_receiving()
            self.controller.update_connection_ui()
            self.controller.aor_status.SetStatusText('Scanning Memory Bank %s' % self.target)

    def restore(self, callback=None):
        if callback is not None:
            self.finish_callback = callback
        if self.phase in ('restoring', 'restore_context'):
            return
        self.cancel_sequence()
        if self.phase == 'restore_failed':
            self.failure = None  # A new explicit Stop attempt has its own outcome.
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
        commands = [self.receiver_command]
        if self.modified:
            self.phase, self.expected_group = 'restoring', self.group
            commands.append(('GM%d\r\n' % self.group).encode('ascii'))
            commands.append(group_members_replacement('scan', self.saved['members']))
            # Preserve ALL returned settings, including valid values outside the GUI range.
            commands.extend((self.saved['raw'][key] + '\r\n').encode('ascii') for key in PARAMETERS)
            commands.append(b'GM\r\n')
        else:
            self.phase, self.expected_group = 'restore_context', self.original['group']
            commands.extend([('GM%d\r\n' % self.original['group']).encode('ascii'), b'GM\r\n'])
        self.send_sequence(commands)
        self.controller.update_connection_ui()

    def fail(self, message):
        discard = getattr(self.controller, 'discard_pending_vfo_target', None)
        if discard is not None:
            discard()
        self.failure = message
        if self.phase in ('restoring', 'restore_context', 'restore_failed'):
            self.restoration_failed(message)
        else:
            self.controller.report_scan_error(message)
            self.restore()

    def restoration_failed(self, message):
        self.cancel_sequence()
        self.failure = message
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
        self.cancel_sequence()
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
