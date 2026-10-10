"""Memory-bank CSV validation and a verified, single-bank replacement transaction."""
import csv
import re
from decimal import Decimal

import wx
from aor_functions import (receiver_frequency_hz, search_step_hz, make_memory_channel_command,
                           memory_channel_read_command, protocol_frequency_hz, protocol_step_hz,
                           display_frequency, display_step, parse_select_scan_response)

REQUIRED = ('Bank', 'Channel', 'Frequency', 'Step', 'Auto', 'Mode', 'Att', 'Skip', 'Selected', 'Name')
COLUMNS = ('Bank', 'BankName', 'Capacity') + REQUIRED[1:]
MODES = ('WFM', 'NFM', 'AM', 'USB', 'LSB', 'CW', 'SFM', 'WAM', 'NAM')


def quantity(value, default_unit):
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s*(Hz|kHz|MHz)?', value.strip(), re.IGNORECASE)
    if match is None:
        raise ValueError('Use a number with Hz, kHz or MHz')
    unit = (match.group(2) or default_unit).lower()
    return Decimal(match.group(1)) * {'hz': 1, 'khz': 1000, 'mhz': 1000000}[unit]


def read_csv(path, bank):
    """Validate every record; return only normalized values, never send commands."""
    rows, labels, extras = {}, set(), []
    with open(path, newline='', encoding='utf-8-sig') as source:
        reader = csv.DictReader(source, strict=True)
        headers = reader.fieldnames or []
        if any(not header.strip() for header in headers) or len(headers) != len(set(headers)) or not set(REQUIRED).issubset(headers):
            raise ValueError('CSV requires unique headers: ' + ', '.join(REQUIRED))
        extras = [column for column in headers if column not in COLUMNS]
        for line, record in enumerate(reader, 2):
            try:
                if None in record or any(value is None for value in record.values()):
                    raise ValueError('Incorrect column count')
                if 'BankName' in record:
                    label = record['BankName'].strip()
                    if len(label) > 8 or any(not 32 <= ord(char) <= 126 for char in label):
                        raise ValueError('BankName requires at most eight printable ASCII characters')
                    labels.add(label)
                if 'Capacity' in record and record['Capacity'].strip():
                    if not re.fullmatch(r'[0-9]+', record['Capacity'].strip()) or not 0 <= int(record['Capacity']) <= 100:
                        raise ValueError('Invalid Capacity (informational only; banks are never resized)')
                channel = record['Channel'].strip()
                if not channel:
                    if any(record[column].strip() for column in REQUIRED[2:]):
                        raise ValueError('An empty-bank summary must have empty channel fields')
                    continue
                match = re.fullmatch(r'(?:[A-Ja-j])?([0-9]{2})', channel)
                if match is None:
                    raise ValueError('Channel must be two digits or a bank letter followed by two digits')
                channel = bank['bank'] + match.group(1)
                if channel in rows:
                    raise ValueError('Duplicate Channel ' + channel)
                if int(channel[1:]) >= bank['channels']:
                    raise ValueError('Channel %s is outside target capacity %d' % (channel, bank['channels']))
                frequency = receiver_frequency_hz(format(quantity(record['Frequency'], 'MHz') / 1000000, 'f'))
                step = search_step_hz(format(quantity(record['Step'], 'kHz') / 1000, 'f'))
                mode = record['Mode'].strip().upper()
                if mode not in MODES:
                    raise ValueError('Invalid Mode: ' + mode)
                flags = {}
                for flag in ('Auto', 'Att', 'Skip', 'Selected'):
                    value = record[flag].strip()
                    if value not in ('0', '1'):
                        raise ValueError(flag + ' must be 0 or 1')
                    flags[flag] = int(value)
                name = record['Name']
                # Validate with the existing MX helper. Include all fields even for Auto:
                # import verifies the supplied Mode/Step instead of silently ignoring them.
                command = make_memory_channel_command(channel, format(Decimal(frequency) / 1000000, 'f'),
                                                      False, MODES.index(mode), format(Decimal(step) / 1000, 'f'),
                                                      flags['Att'], name)
                if flags['Auto']:
                    command = command.replace(b' AU0 ', b' AU1 ', 1)
                rows[channel] = dict(flags, Frequency=frequency, Step=step, Mode=MODES.index(mode),
                                     Name=name.rstrip(), command=command,
                                     extra={column: record[column] for column in extras})
            except ValueError as error:
                raise ValueError('CSV line %d: %s' % (line, error)) from error
    if len(labels) > 1:
        raise ValueError('CSV contains conflicting BankName values')
    return {'bank': bank['bank'], 'capacity': bank['channels'], 'rows': rows,
            'label': next(iter(labels)) if labels else None, 'extra_columns': extras}


def export_record(bank, fields, selected):
    if fields is None:
        return dict(zip(COLUMNS, (bank['bank'], bank['name'], bank['channels']) + ('',) * 9))
    return dict(zip(COLUMNS, (bank['bank'], bank['name'], bank['channels'], fields[0],
                             display_frequency(protocol_frequency_hz(fields[2])),
                             display_step(protocol_step_hz(fields[3])), fields[4], MODES[int(fields[5])],
                             fields[6], fields[1], int(fields[0] in selected), fields[7])))


def mismatch(channel, expected, fields, selected):
    if fields is None:
        return '%s: Channel expected populated, received empty' % channel
    actual = dict(Frequency=protocol_frequency_hz(fields[2]), Step=protocol_step_hz(fields[3]),
                  Auto=int(fields[4]), Mode=int(fields[5]), Att=int(fields[6]), Skip=int(fields[1]),
                  Selected=int(channel in selected), Name=fields[7].rstrip())
    for field in ('Frequency', 'Step', 'Auto', 'Mode', 'Att', 'Skip', 'Selected', 'Name'):
        if actual[field] != expected[field]:
            return '%s: %s expected %r, received %r' % (channel, field, expected[field], actual[field])
    return None


class BankImport:
    """One GUI-event-driven transaction; all serial I/O uses the existing controller."""
    def __init__(self, controller, plan):
        self.c = controller
        self.plan = plan
        self.bank = plan['bank']
        self.phase = 'preflight'
        self.sending = False
        self.original = None
        self.protection = None
        self.restore_needed = False
        self.wp = None
        self.wm = {}
        self.error = None
        self.timer = wx.Timer(controller)
        controller.Bind(wx.EVT_TIMER, self.on_timeout, self.timer)
        self.index = 0
        self.channels = sorted(plan['rows'])
        self.selected = set()
        self.final_snapshot = None
        self.restored = False

    def start(self):
        self.c.start_memory_inventory(None, bank=self.bank, on_complete=self.prepare, on_error=self.fail)

    def prepare(self, snapshot):
        metadata = snapshot['banks'][self.bank]
        if metadata['channels'] != self.plan['capacity']:
            self.finish('Import cancelled: target capacity changed; validate the CSV again.')
            return
        context = snapshot['context']
        if not context.startswith(('VA ', 'VB ', 'VF ', 'MR ')):
            self.finish('Import cancelled: stop scanning/searching before replacing a bank.')
            return
        if context.startswith('MR MX' + self.bank):
            self.finish('Import cancelled: switch to a VFO before replacing the currently recalled bank.')
            return
        self.original = context
        self.restore = ((context[:2] + '\r\n') if context.startswith(('VA ', 'VB ', 'VF ')) else
                        ('MR%s\r\n' % context.split()[1][2:])).encode('ascii')
        self.outside_selected = {channel for channel in snapshot['selected'] if channel[0] != self.bank}
        desired = {channel for channel, row in self.plan['rows'].items() if row['Selected']}
        if len(self.outside_selected | desired) > 100:
            self.finish('Import cancelled: Selected Channels would exceed the documented 100-channel limit.')
            return
        label = self.plan['label']
        self.label = metadata['name'] if label is None else label
        if len(self.label) > 8 or any(not 32 <= ord(char) <= 126 for char in self.label):
            self.finish('Import cancelled: current bank label is outside the documented eight-character ASCII format.')
            return
        message = ('Target bank: %s\nCurrent label: %s\nImported label: %s\nEntries: %d\nCapacity: %d\n'
                   'Overwrite mode: Replace bank\n\nAll remaining channels in this bank will be cleared.\n'
                   'Other banks will be preserved. Continue?' %
                   (self.bank, metadata['name'] or '(unnamed)', '(unchanged)' if label is None else label or '(unnamed)',
                    len(self.channels), metadata['channels']))
        if wx.MessageBox(message, 'Import Memory Bank CSV', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self.c) != wx.YES:
            self.finish('Import cancelled; no scanner writes.')
            return
        self.c.memory_bank_transfer = self
        self.c.update_connection_ui()
        self.query_protection('capture_protection')

    def send(self, data):
        self.last_command = data
        self.timer.StartOnce(20000)
        self.sending = True
        try:
            result = self.c.write_serial(data)
        finally:
            self.sending = False
        if result != len(data):
            self.fail('Serial write failed during %s%s' % (self.phase, ' ' + self.channel if hasattr(self, 'channel') else ''))
            return False
        return True

    def query_protection(self, phase, prefix=b''):
        self.phase = phase
        self.wp, self.wm = None, {}
        self.c.aor_status.SetStatusText({'capture_protection': 'Import: reading write-protection state...',
                                       'unprotect': 'Import: preparing target bank %s...' % self.bank,
                                       'cleanup': 'Import: restoring receiver and write protection...'}[phase])
        self.send(prefix + b'WP\r\nWM\r\nWM\r\nRX\r\n')

    def read_bank(self, callback):
        self.timer.Stop()
        self.phase = 'read_bank'
        self.c.start_memory_inventory(None, bank=self.bank, on_complete=callback, on_error=self.fail, owner=self)

    def receive(self, text):
        if text == '?':
            self.fail('AR8600 returned ? for bank %s during %s%s; command %r' %
                      (self.bank, self.phase, ' ' + self.channel if hasattr(self, 'channel') else '', self.last_command))
            return True
        if self.c.memory_inventory is not None:
            return False
        try:
            if text.startswith('WP'):
                match = re.fullmatch(r'WP([01])', text)
                if match is None:
                    raise ValueError('Malformed global write-protection response: ' + text)
                self.wp = int(match.group(1))
                return True
            if text.startswith('WM'):
                match = re.fullmatch(r'WM\s*([A-Ja-j])([01])', text)
                if match is None:
                    raise ValueError('Malformed bank write-protection response: ' + text)
                bank, value = match.groups()
                if bank in self.wm and self.wm[bank] != int(value) and self.phase != 'cleanup':
                    raise ValueError('Conflicting bank write-protection response: ' + text)
                self.wm[bank] = int(value)
                return True
            if text.startswith('GR') and self.phase == 'verify_selected':
                entry = parse_select_scan_response(text)
                slot = int(entry['slot'])
                if slot in self.slots and self.slots[slot] != entry['channel']:
                    raise ValueError('Conflicting GR slot')
                self.slots[slot] = entry['channel']
                if entry['channel'] is not None:
                    self.read_selected.add(entry['channel'])
                return True
            if text.startswith('MX') and self.phase == 'verify_write':
                channel, fields = self.c.parse_memory_line(text)
                if channel in self.remaining:
                    self.remaining.remove(channel)
                    if channel == self.channel:
                        self.fields = fields
                    if not self.remaining:
                        self.verify_channel()
                return True
            if text.startswith(('VA ', 'VB ', 'VF ', 'MR ')):
                if text.startswith('MR '):
                    self.c.parse_memory_line(text[3:])
                else:
                    self.c.validate_fields(text.split()[1:], ('RF', 'ST', 'AU', 'MD', 'AT'))
                self.context(text)
                return True
            if text in ('MC0', 'MC1', 'GA0', 'GA1', 'MP0', 'MP1'):
                return True
        except (ValueError, IndexError, TypeError) as error:
            self.fail(str(error))
            return True
        return False

    def context(self, text):
        if self.phase in ('capture_protection', 'unprotect', 'cleanup'):
            if self.wp is None or self.bank not in self.wm:
                return  # An earlier queued RX is not our protection-query fence.
            if self.phase == 'capture_protection':
                if text != self.original:
                    self.fail('Receiver context changed before import; nothing written.')
                    return
                self.protection = (self.wp, self.wm[self.bank])
                self.restore_needed = True  # Include partial/failed protection writes in cleanup.
                self.query_protection('unprotect', ('WP0\r\nWM%s0\r\n' % self.bank).encode('ascii'))
            elif self.phase == 'unprotect':
                if self.wp != 0 or self.wm[self.bank] != 0 or text != self.original:
                    self.fail('Write protection/context did not match the requested pre-write state.')
                    return
                self.phase = 'clear'
                if self.send(('MQ%s%%%%\r\n' % self.bank).encode('ascii')):
                    self.read_bank(self.after_clear)
            else:
                if (self.wp, self.wm[self.bank]) != self.protection or text != self.original:
                    return  # Drain earlier replies; await verified restoration or the watchdog.
                self.restored = True
                self.finish(self.error or 'Import complete: bank %s, %d channels saved and verified; receiver/protection restored.' %
                            (self.bank, len(self.channels)), success=self.error is None)
        elif self.phase == 'recall_flags':
            if not text.startswith('MR MX' + self.channel + ' '):
                self.fail(self.channel + ': temporary memory recall was not confirmed')
                return
            self.fields = self.c.parse_memory_line(text[3:])[1]
            expected = self.plan['rows'][self.channel]
            if int(self.fields[1]) != expected['Skip']:
                self.phase = 'verify_skip'
                self.send(('MP%d\r\nRX\r\n' % expected['Skip']).encode('ascii'))
            else:
                self.set_selected_or_restore()
        elif self.phase == 'verify_skip':
            if not text.startswith('MR MX' + self.channel + ' '):
                self.fail(self.channel + ': Skip verification received a different context')
                return
            self.fields = self.c.parse_memory_line(text[3:])[1]
            if int(self.fields[1]) != self.plan['rows'][self.channel]['Skip']:
                self.fail('%s: Skip expected %d, received %s' % (self.channel, self.plan['rows'][self.channel]['Skip'], self.fields[1]))
                return
            self.set_selected_or_restore()
        elif self.phase in ('verify_selected', 'restore_flags'):
            if text != self.original:
                self.fail(self.channel + ': original receiver context was not restored')
                return
            if self.phase == 'verify_selected':
                if not self.slots or set(self.slots) != set(range(max(self.slots) + 1)):
                    self.fail(self.channel + ': incomplete Selected GR read-back')
                    return
                desired = set(self.selected)
                if self.plan['rows'][self.channel]['Selected']:
                    desired.add(self.channel)
                else:
                    desired.discard(self.channel)
                if self.read_selected != desired:
                    self.fail(self.channel + ': Selected membership differs from requested GR state')
                    return
                self.selected = self.read_selected
            self.c.monitor_muted = False
            issue = mismatch(self.channel, self.plan['rows'][self.channel], self.fields, self.selected)
            if issue:
                self.fail(issue)
                return
            self.next_channel()

    def after_clear(self, snapshot):
        if snapshot['rows']:
            self.fail('%s: clear failed; channel still populated (possibly individually protected)' % sorted(snapshot['rows'])[0])
            return
        if snapshot['selected'] != self.outside_selected:
            self.fail('Clear read-back changed unrelated Selected membership or left target entries selected.')
            return
        self.selected = set(snapshot['selected'])
        self.write_channel()

    def write_channel(self):
        if self.index == len(self.channels):
            self.phase = 'label'
            if not self.send(('TB%s%s\r\n' % (self.bank, self.label.ljust(8))).encode('ascii')):
                return
            self.read_bank(self.verify_bank)
            return
        self.channel = self.channels[self.index]
        self.phase = 'write'
        self.c.aor_status.SetStatusText('Import %s: writing %s (%d/%d)...' %
                                       (self.bank, self.channel, self.index + 1, len(self.channels)))
        if not self.send(self.plan['rows'][self.channel]['command']):
            return
        self.phase = 'verify_write'
        self.fields = None
        self.remaining = {'%s%02d' % (self.bank, number) for number in range((int(self.channel[1:]) // 10 + 1) * 10)}
        self.send(memory_channel_read_command(self.channel))

    def verify_channel(self):
        expected = self.plan['rows'][self.channel]
        # Flags are verified after their separate GA/MP transaction.
        check = dict(expected, Skip=int(self.fields[1]) if self.fields else 0,
                     Selected=int(self.channel in self.selected))
        issue = mismatch(self.channel, check, self.fields, self.selected)
        if issue:
            self.fail(issue)
            return
        if int(self.fields[1]) != expected['Skip'] or int(self.channel in self.selected) != expected['Selected']:
            self.phase = 'recall_flags'
            self.c.monitor_muted = True
            self.send(('MC1\r\nMR%s\r\nRX\r\n' % self.channel).encode('ascii'))
        else:
            self.next_channel()

    def set_selected_or_restore(self):
        expected = self.plan['rows'][self.channel]['Selected']
        command = self.restore + b'MC0\r\n'
        if int(self.channel in self.selected) != expected:
            self.phase = 'verify_selected'
            self.slots, self.read_selected = {}, set()
            command = ('GA%d\r\n' % expected).encode('ascii') + command + b'GR\r\nRX\r\n'
        else:
            self.phase = 'restore_flags'
            command += b'RX\r\n'
        self.send(command)

    def next_channel(self):
        self.index += 1
        self.write_channel()

    def verify_bank(self, snapshot):
        expected_rows = self.plan['rows']
        metadata = snapshot['banks'][self.bank]
        if metadata['channels'] != self.plan['capacity']:
            self.fail(self.bank + ': Capacity changed unexpectedly')
            return
        if metadata['name'] != self.label:
            self.fail('%s: BankName expected %r, received %r' % (self.bank, self.label, metadata['name']))
            return
        difference = set(expected_rows).symmetric_difference(snapshot['rows'])
        if difference:
            channel = sorted(difference)[0]
            self.fail(channel + ': expected ' + ('populated' if channel in expected_rows else 'empty') + ', bank read-back differs')
            return
        for channel, expected in expected_rows.items():
            issue = mismatch(channel, expected, snapshot['rows'][channel], snapshot['selected'])
            if issue:
                self.fail(issue)
                return
        desired = self.outside_selected | {channel for channel, row in expected_rows.items() if row['Selected']}
        if snapshot['selected'] != desired:
            self.fail('Final Selected membership differs, including entries outside the target bank.')
            return
        self.final_snapshot = snapshot
        self.cleanup()

    def cleanup(self):
        if self.c.memory_inventory is not None:
            self.c.memory_inventory['on_error'] = None
            self.c.finish_memory_inventory('Import: restoring receiver/protection...')
        if not self.restore_needed:
            self.finish(self.error or 'Import stopped before writing.')
            return
        if not self.c.serial.is_open:
            self.finish((self.error or 'Import interrupted') + '; disconnected, receiver/protection restoration could not be verified.')
            return
        global_state, bank_state = self.protection
        prefix = ('WM%s%d\r\nWP%d\r\n' % (self.bank, bank_state, global_state)).encode('ascii') + self.restore + b'MC0\r\n'
        self.c.monitor_muted = False
        self.query_protection('cleanup', prefix)
        if self.c.memory_bank_transfer is self and not self.c.alive.is_set():
            self.finish((self.error or 'Import interrupted') + '; restoration sent, but receiver stopped before verification.')

    def fail(self, message):
        if self.phase == 'cleanup':
            self.finish((self.error or 'Import interrupted') + '; restoration could not be verified: ' + message)
            return
        self.error = 'Import failed: ' + message + ' No further memory writes will be sent.'
        self.cleanup()

    def abort_disconnect(self):
        self.error = 'Import interrupted by disconnect; bank may be partially replaced.'
        self.cleanup()
        if self.c.memory_bank_transfer is self:
            self.finish(self.error + ' Restoration sent but could not be verified before disconnect.')

    def on_timeout(self, event):
        detail = ''
        if self.phase in ('capture_protection', 'unprotect', 'cleanup'):
            detail = '; bank %s, WP=%r, WM=%r' % (self.bank, self.wp, self.wm.get(self.bank))
        self.fail('Timeout during %s%s%s' % (self.phase, ' ' + self.channel if hasattr(self, 'channel') else '', detail))

    def finish(self, message, success=False):
        self.timer.Stop()
        self.c.Unbind(wx.EVT_TIMER, source=self.timer)
        if self.restore_needed and not self.restored and self.c.serial.is_open:
            # A failed/partial cleanup write must not leave temporary audio muting
            # unaddressed. This is a bounded, best-effort release, never a retry loop.
            self.sending = True
            try:
                self.c.write_serial(b'MC0\r\n')
            finally:
                self.sending = False
        if self.c.memory_bank_transfer is self:
            self.c.memory_bank_transfer = None
        self.c.update_connection_ui()
        if success:
            self.c.memory_csv_metadata[self.bank] = {'columns': self.plan['extra_columns'],
                                                    'channels': {channel: row['extra'] for channel, row in self.plan['rows'].items()}}
            metadata = self.final_snapshot['banks'][self.bank]
            self.c.set_memory_banks_list('MW %s:%d TB%s%s' % (self.bank, metadata['channels'], self.bank, metadata['name']))
            for index in range(self.c.cbx_lists.GetCount()):
                item = self.c.cbx_lists.GetClientData(index)
                if item is not None and item['bank'] == self.bank:
                    self.c.cbx_lists.SetSelection(index)
                    self.c.on_select_list(None)
                    break
        if self.original and self.restored and self.c.serial.is_open:
            if self.original.startswith(('VA ', 'VB ', 'VF ')):
                self.c.set_vfo_text(self.original, {'VA': 0, 'VB': 1, 'VF': 2}[self.original[:2]])
            self.c.receive_context_status(self.original)
        self.c.aor_status.SetStatusText(message)
        if message.startswith('Import failed') or 'could not be verified' in message or 'verification failed' in message:
            import sys
            print(message, file=sys.stderr)
