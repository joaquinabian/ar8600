"""Paced, reversible Search Group context for one stored frequency range."""
import re

from aor_functions import group_members_replacement, parse_group_members, parse_operation_parameter, operation_parameter_command
from current_bank_scan import CurrentBankScan

PARAMETERS = ('SA', 'SB', 'SD', 'SP')
PARAMETER_PATTERN = r'S[AB][ +]?[0-9]{3}|SD(?:[0-9]\.[0-9]|[0-9]{2}|FF)|SP[0-9]{2}'
DEFAULT_PROFILE = dict(SA=0, SB=None, SD=20, SP=0)


def search_group_snapshot(header, membership):
    match = re.match(r'GS([0-9])(?:\s|$)', header)
    if not match:
        raise ValueError('Invalid GS response')
    text = header[match.end():]
    # AS is reported by GS but is not changed by this operation.
    if re.sub(PARAMETER_PATTERN + r'|AS[01]', '', text).strip():
        raise ValueError('Unrecognized/incomplete GS parameter data')
    raw, values = {}, {}
    for item in re.finditer(PARAMETER_PATTERN, text):
        key, value = parse_operation_parameter(item.group())
        if key in values:
            raise ValueError('Duplicate Search Group parameter')
        values[key] = value
        raw[key] = ('SP%02d' % value if key == 'SP' else
                    operation_parameter_command(key, value).decode('ascii').rstrip('\r\n'))
    if set(values) != set(PARAMETERS):
        raise ValueError('Incomplete GS settings; SA/SB/SD/SP are required')
    parse_group_members(membership, 'search')
    members = tuple(membership[2:].strip().replace('-', ''))
    return dict(group=int(match[1]), members=members, raw=raw, values=values)


class StoredRangeSearch(CurrentBankScan):
    """Reuse the existing pacing, bounded failure handling and lifecycle callbacks."""
    kind = 'search'
    verification_command = b'GS\r\n'

    def __init__(self, controller, target, level):
        super().__init__(controller, target, level)
        self.profile = dict(DEFAULT_PROFILE)
        self.profile.update(controller.stored_range_profiles.get(self.target, {}))
        self.level = level if self.profile['SB'] is None else self.profile['SB']
        self.restoring_receiver = False

    def start(self):
        if not isinstance(self.level, int) or not 0 <= self.level <= 255:
            self.fail('Read confirmed listening Squelch (DB) before starting Stored Range.')
            return
        self.query_group(None, 'original')

    def command_diagnostic(self, command=None):
        if self.phase in ('original', 'probe'):
            context = ('the current Search Group' if self.expected_group is None else
                       'Search Group %d' % self.expected_group)
            action = 'reading'
        else:
            group = self.original['group'] if self.phase == 'restore_context' else self.group
            context = '%s Search Group %d' % ('original' if self.phase == 'restore_context' else 'temporary', group)
            action = 'configuring' if self.phase == 'configured' else 'starting' if self.phase == 'starting' else 'restoring'
        return '%s while %s %s' % ((command or self.current_command).decode('ascii').rstrip('\r\n'), action, context)

    def timeout(self):
        self.fail('Temporary Search Group read-back timed out during %s.' % self.phase)

    def query_group(self, group, phase):
        self.phase, self.header, self.expected_group = phase, None, group
        self.controller.aor_status.SetStatusText('%s Stored Range: reading Search Group %s...' %
                                               ('Restoring' if phase == 'restore_context' else 'Preparing',
                                                'current' if group is None else group))
        commands = ([('GS%d\r\n' % group).encode('ascii')] if group is not None else []) + [b'GS\r\n']
        self.send_sequence(commands)

    def send_next_command(self, generation):
        super().send_next_command(generation)
        if (generation == self.sequence_generation and self.restoring_receiver and
                self.phase == 'restore_context' and not self.command_queue and self.current_command == b'RX\r\n'):
            self.failure = None
            self.finish('Stored Range stopped; original Search Group restored and verified.')

    def receive(self, text):
        if self.phase == 'finished':
            return False
        if text == '?':
            if self.busy or self.controller.level_squelch_pending:
                self.fail('Scanner rejected %s.' % self.command_diagnostic() if self.current_command is not None else
                          'Scanner rejected temporary Search Group operation during ' + self.phase)
            return True
        try:
            if self.phase == 'starting' and re.match(r'SR[A-Ta-t] RF', text):
                if text[2] != self.target:
                    raise ValueError('Search started in another Stored Range')
                fields = text.split(None, 6)
                self.controller.validate_fields(fields[1:6], ('RF', 'ST', 'AU', 'MD', 'AT'))
                if len(fields) != 7 or not fields[6].startswith('TT'):
                    raise ValueError('Invalid active Stored Range status')
                if self.command_queue or self.current_command != b'RX\r\n':
                    return True  # Wait for the paced RX before accepting the start read-back.
                self.cancel_sequence()
                self.phase = 'active'
                self.timer.Stop()
                self.controller.refresh_level_context(query=False)
                self.controller.update_connection_ui()
                return False
            if self.busy and text.startswith(('GS', 'BS')):
                if self.restoring_receiver or not self.awaiting_final_group:
                    return True  # Selection/setter echoes are not final verification.
                if text.startswith('GS'):
                    if not re.match(r'GS[0-9](?:\s|$)', text):
                        raise ValueError('Invalid Search Group header')
                    if self.expected_group is not None and int(text[2]) != self.expected_group:
                        raise ValueError('Scanner returned a different Search Group')
                    self.header = text
                elif self.header is None:
                    parse_group_members(text, 'search')
                else:
                    self.accept_group(search_group_snapshot(self.header, text))
                return True
            if self.busy and text.startswith(PARAMETERS):
                parse_operation_parameter(text)
                return True
        except (ValueError, IndexError, TypeError) as error:
            self.fail('Invalid temporary Search Group read-back: %s' % error)
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
            if state['members'] != (self.target,) or state['values'] != dict(self.profile, SB=self.level):
                raise ValueError('Temporary Search Group membership/settings did not verify')
            self.cancel_sequence()
            self.controller.search_levels[self.group] = state['values']['SB']
            self.controller.group_response['search'] = self.group
            self.begin_scan()
        elif self.phase == 'restoring':
            if state['members'] != self.saved['members'] or state['values'] != self.saved['values']:
                raise ValueError('Original borrowed Search Group did not verify')
            self.modified = False
            self.controller.search_levels[self.group] = state['values']['SB']
            self.query_group(self.original['group'], 'restore_context')
        elif self.phase == 'restore_context':
            if state['members'] != self.original['members'] or state['values'] != self.original['values']:
                raise ValueError('Originally selected Search Group did not verify')
            self.controller.group_response['search'] = state['group']
            self.controller.cbx_search.SetStringSelection(str(state['group']))
            self.controller.search_levels[state['group']] = state['values']['SB']
            self.restoring_receiver = True
            self.send_sequence([self.receiver_command, b'RX\r\n'])

    def configure(self, state):
        self.group, self.saved, self.modified = state['group'], state, True
        self.phase, self.expected_group, self.header = 'configured', self.group, None
        self.controller.aor_status.SetStatusText('Preparing Stored Range: configuring temporary Search Group %d...' % self.group)
        values = dict(self.profile, SB=self.level)
        self.send_sequence([('GS%d\r\n' % self.group).encode('ascii'),
                            group_members_replacement('search', self.target)] +
                           [operation_parameter_command(key, values[key]) for key in PARAMETERS] + [b'GS\r\n'])

    def begin_scan(self):
        self.phase = 'starting'
        self.controller.active_operation = ('Frequency Search', 'Search Bank')
        self.controller.operation_signal_open = False
        self.controller.level_vfo_context = False
        self.controller.refresh_level_context(query=False)
        self.send_sequence([('GS%d\r\n' % self.group).encode('ascii'),
                            ('SS%s\r\n' % self.target).encode('ascii'), b'RX\r\n'])
        self.controller.show_now_receiving()
        self.controller.update_connection_ui()

    def restore(self, callback=None):
        if callback is not None:
            self.finish_callback = callback
        if self.phase in ('restoring', 'restore_context'):
            return
        self.cancel_sequence()
        if self.phase == 'restore_failed':
            self.failure = None
        if self.timer is not None:
            self.timer.Stop()
        dialog = getattr(self.controller, 'settings_dialog', None)
        if dialog is not None and dialog.family == 'S' and dialog.group == self.group:
            dialog.Close()
        self.controller.active_operation = None
        self.controller.operation_signal_open = False
        self.controller.clear_level_edits()
        if self.original is None:
            self.finish(self.failure or 'Stored Range preparation cancelled; groups unchanged.')
            return
        if not self.controller.serial.is_open:
            self.restoration_failed('Serial port unavailable; Search Group restoration was not verified.')
            return
        self.restoring_receiver = False
        self.header = None
        if self.modified:
            self.phase, self.expected_group = 'restoring', self.group
            commands = [self.receiver_command, ('GS%d\r\n' % self.group).encode('ascii'),
                        group_members_replacement('search', self.saved['members'])]
            commands += [(self.saved['raw'][key] + '\r\n').encode('ascii') for key in PARAMETERS]
            commands.append(b'GS\r\n')
            self.send_sequence(commands)
        else:
            self.query_group(self.original['group'], 'restore_context')
        self.controller.update_connection_ui()
