"""Focused temporary Search Group tests; no serial hardware or scanner writes."""
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock

from test_current_bank_scan import Controller, GuiBoundaryTests, main, drain_pacing
from stored_range_search import StoredRangeSearch, search_group_snapshot
import test_scan_controls as scan_controls


def gs(group, level=7, voice=23, delay='3.0', dwell=9):
    return 'GS%d SD%s SB %03d SA %03d SP%02d AS0' % (group, delay, level, voice, dwell)


def bs(members):
    return 'BS ' + ''.join(bank if bank in members else '-' for bank in 'ABCDEFGHIJKLMNOPQRSTabcdefghijklmnopqrst')


ACTIVE = 'SRA RF0005970000 ST005000 AU0 MD2 AT0 TTStation'


def controller():
    ctrl = GuiBoundaryTests().level_controller()
    ctrl.stored_range_profiles = {}
    ctrl.search_levels = {}
    ctrl.search_banks = {'A': dict(bank='A', empty=False, name='Shortwave')}
    ctrl.group_response['search'] = 0
    ctrl.cbx_search = Mock()
    ctrl.cbx_search.GetValue.return_value = '3'
    ctrl.group_dialogs = {'scan': None, 'search': None}
    ctrl.memory_channel_pending = ctrl.memory_select_read = ctrl.memory_flag_edit = None
    ctrl.filling_banks = False
    ctrl.bandscope = None
    ctrl.scan_lifecycle_action = None
    ctrl.selected_search_bank = lambda: 'A'
    ctrl.operation_selection = lambda: ('Frequency Search', 'Search Bank')
    ctrl.level_squelch_value = 255
    ctrl.update_connection_ui = Mock()
    ctrl.update_operation_ui = Mock()
    ctrl.search_restore_command = None
    ctrl.pending_vfo_target = ctrl.pending_vfo_timer = None
    ctrl.pending_vfo_applying = False
    ctrl.operation_signal_open = False
    ctrl.discard_pending_vfo_target = Mock()
    ctrl.mirror_console_line = Mock()
    ctrl.database_station = None
    for method in ('on_start_search_bank', 'restore_temporary_scan', 'on_operation_stop', 'finish_operation_stop'):
        setattr(ctrl, method, MethodType(getattr(main.AorCtrl, method), ctrl))
    return ctrl


def feed(scan, group, members, **values):
    drain_pacing(scan)
    scan.receive(gs(group, **values))
    scan.receive(bs(members))


def prepare(ctrl=None):
    ctrl = ctrl or controller()
    ctrl.on_start_search_bank(None, bank='A')
    scan = ctrl.temporary_scan
    feed(scan, 0, '', level=0, voice=0, delay='2.0', dwell=0)
    feed(scan, 1, 'B')
    feed(scan, 2, '')
    return ctrl, scan


def activate(ctrl, scan):
    values = dict(scan.profile, SB=scan.level)
    delay = 'FF' if values['SD'] == 'Hold' else '%.1f' % (values['SD'] / 10)
    feed(scan, scan.group, 'A', level=values['SB'], voice=values['SA'], delay=delay, dwell=values['SP'])
    drain_pacing(scan)
    scan.receive(ACTIVE)


def complete_restore(scan):
    feed(scan, 2, '')
    feed(scan, 0, '', level=0, voice=0, delay='2.0', dwell=0)
    drain_pacing(scan)


class StoredRangeTests(unittest.TestCase):
    def test_idle_stored_range_squelch_reads_db_not_sb(self):
        ctrl = controller()
        self.assertEqual(ctrl.level_context(), ('DB', None))
        ctrl.refresh_level_context()
        self.assertEqual(ctrl.commands, [b'DB\r\n'])

    def test_capture_db_and_single_bank_paced_configuration_without_group_zero_writes(self):
        ctrl, scan = prepare()
        self.assertEqual(scan.group, 2)
        self.assertEqual(scan.saved['values'], dict(SA=23, SB=7, SD=30, SP=9))
        start = len(ctrl.commands) - 1
        self.assertEqual(ctrl.commands[-1], b'GS2\r\n')
        self.assertEqual(scan.pace_timer.milliseconds, 200)
        drain_pacing(scan)
        self.assertEqual(ctrl.commands[start:], [b'GS2\r\n', b'BS%% A\r\n', b'SA000\r\n', b'SB255\r\n',
                                                b'SD20\r\n', b'SP00\r\n', b'GS\r\n'])
        self.assertIsNone(ctrl.active_operation)
        self.assertFalse(any(b'SS' in c or b'MA' in c or b'SE' in c for c in ctrl.commands))
        activate(ctrl, scan)
        self.assertEqual(scan.phase, 'active')
        self.assertEqual(ctrl.commands[-3:], [b'GS2\r\n', b'SSA\r\n', b'RX\r\n'])
        self.assertEqual(ctrl.level_context(), ('SB', 2))
        self.assertEqual(ctrl.displayed_level(), 255)
        self.assertEqual(ctrl.level_squelch_value, 255)

    def test_live_slider_writes_sb_and_confirmed_reply_updates_search_cache(self):
        ctrl, scan = prepare()
        activate(ctrl, scan)
        ctrl.commands.clear()
        ctrl.level_squelch_requested = 44
        main.AorCtrl.on_level_squelch_timer(ctrl, None)
        self.assertEqual(ctrl.commands, [b'SB044\r\nSB\r\n'])
        ctrl.accept_operation_parameter('SB 044')
        self.assertEqual(ctrl.search_levels[2], 44)
        self.assertEqual(ctrl.level_squelch_value, 255)
        self.assertFalse(ctrl.level_squelch_pending)

    def test_stop_restores_members_settings_selected_group_and_queries_db(self):
        ctrl, scan = prepare()
        activate(ctrl, scan)
        ctrl.commands.clear()
        ctrl.on_operation_stop(None)
        drain_pacing(scan)
        self.assertEqual(ctrl.commands, [b'VA\r\n', b'GS2\r\n', b'BS%%\r\n', b'SA023\r\n',
                                         b'SB007\r\n', b'SD30\r\n', b'SP09\r\n', b'GS\r\n'])
        complete_restore(scan)
        self.assertIsNone(ctrl.temporary_scan)
        self.assertEqual(ctrl.group_response['search'], 0)
        self.assertEqual(ctrl.level_context(), ('DB', None))
        self.assertEqual(ctrl.commands[-5:], [b'GS0\r\n', b'GS\r\n', b'VA\r\n', b'RX\r\n', b'DB\r\n'])
        self.assertFalse(ctrl.errors)

    def test_start_stop_start_and_session_explicit_override(self):
        ctrl = controller()
        ctrl.stored_range_profiles['A'] = dict(SA=4, SB=42, SD=15, SP=10)
        ctrl, scan = prepare(ctrl)
        activate(ctrl, scan)
        self.assertEqual(ctrl.search_levels[2], 42)
        ctrl.on_operation_stop(None)
        complete_restore(scan)
        ctrl.accept_operation_parameter('DB 255')
        ctrl, next_scan = prepare(ctrl)
        activate(ctrl, next_scan)
        self.assertEqual(next_scan.level, 42)
        self.assertEqual(next_scan.profile, dict(SA=4, SB=42, SD=15, SP=10))
        self.assertEqual(next_scan.phase, 'active')

    def test_linked_ranges_uses_selected_sb_without_borrowing_and_manual_range_keeps_db(self):
        ctrl = controller()
        ctrl.active_operation = ('Frequency Search', 'Search Group')
        ctrl.group_response['search'] = 3
        ctrl.search_levels[3] = 12
        self.assertEqual(ctrl.level_context(), ('SB', 3))
        ctrl.refresh_level_context()
        self.assertEqual(ctrl.commands, [b'SB\r\n'])
        ctrl.accept_operation_parameter('SB 012')
        ctrl.commands.clear()
        ctrl.level_squelch_requested = 13
        main.AorCtrl.on_level_squelch_timer(ctrl, None)
        self.assertEqual(ctrl.commands, [b'SB013\r\nSB\r\n'])
        self.assertIsNone(ctrl.temporary_scan)
        ctrl.clear_level_edits()
        ctrl.active_operation = ('Frequency Search', 'Range')
        self.assertEqual(ctrl.level_context(), ('DB', None))
        ctrl.commands.clear()
        ctrl.level_squelch_requested = 14
        main.AorCtrl.on_level_squelch_timer(ctrl, None)
        self.assertEqual(ctrl.commands, [b'DB014\r\nDB\r\n'])

    def test_all_occupied_groups_fall_back_to_one_and_never_edit_zero(self):
        ctrl = controller()
        ctrl.on_start_search_bank(None)
        scan = ctrl.temporary_scan
        feed(scan, 0, '', level=0, voice=0, delay='2.0', dwell=0)
        for group in range(1, 10):
            feed(scan, group, 'BC')
        self.assertEqual(scan.group, 1)
        self.assertEqual(scan.saved['members'], ('B', 'C'))
        drain_pacing(scan)
        self.assertIn(b'BS%% A\r\n', ctrl.commands)
        self.assertNotIn(b'GS0\r\n', ctrl.commands)  # Initial group 0 was only queried with GS.
        activate(ctrl, scan)
        ctrl.on_operation_stop(None)
        drain_pacing(scan)
        self.assertIn(b'BS%% BC\r\n', ctrl.commands)
        feed(scan, 1, 'BC')
        feed(scan, 0, '', level=0, voice=0, delay='2.0', dwell=0)
        drain_pacing(scan)
        self.assertIsNone(ctrl.temporary_scan)

    def test_rejection_identifies_exact_command_and_restores(self):
        ctrl, scan = prepare()
        for n in range(3):
            scan.pace_timer.Fire()
        self.assertEqual(scan.current_command, b'SB255\r\n')
        scan.receive('?')
        self.assertIn('Scanner rejected SB255 while configuring temporary Search Group 2.', ctrl.errors)
        self.assertEqual(scan.phase, 'restoring')
        complete_restore(scan)
        self.assertIsNone(ctrl.temporary_scan)

    def test_original_query_rejection_fails_safely_before_any_group_is_changed(self):
        ctrl = controller()
        ctrl.on_start_search_bank(None)
        ctrl.temporary_scan.receive('?')
        self.assertIsNone(ctrl.temporary_scan)
        self.assertEqual(ctrl.commands, [b'GS\r\n'])
        self.assertIn('Scanner rejected GS while reading the current Search Group.', ctrl.errors)

    def test_snapshot_retains_hold_and_wide_original_dwell_and_optional_as(self):
        state = search_group_snapshot('GS3 SDFF SB+255 SA 001 SP99 AS0', bs('aC'))
        self.assertEqual(state['raw'], dict(SA='SA001', SB='SB255', SD='SDFF', SP='SP99'))
        self.assertEqual(state['members'], ('C', 'a'))

    def test_pending_to_vfo_waits_for_search_group_and_receiver_restoration(self):
        original, scan = prepare()
        activate(original, scan)
        ctrl = scan_controls.controller(original)
        ctrl.active_operation = ('Frequency Search', 'Search Bank')
        ctrl.receiver_status = dict(channel=None, frequency_hz=5970000, step_hz=5000, mode=2, attenuator=1)
        ctrl.operation_signal_open = True
        ctrl.on_scan_to_vfo(None)
        ctrl.commands.clear()
        ctrl.on_operation_stop(None)
        drain_pacing(scan)
        self.assertFalse(any(command == b'VF\r\n' or command.startswith(b'RF') for command in ctrl.commands))
        feed(scan, 2, '')
        feed(scan, 0, '', level=0, voice=0, delay='2.0', dwell=0)
        self.assertIsNotNone(ctrl.temporary_scan)
        self.assertFalse(any(command == b'VF\r\n' for command in ctrl.commands))
        drain_pacing(scan)
        self.assertIsNone(ctrl.temporary_scan)
        index = ctrl.commands.index(b'MC1\r\n')
        self.assertEqual(ctrl.commands[index - 4:index], [b'GS0\r\n', b'GS\r\n', b'VA\r\n', b'RX\r\n'])
        self.assertEqual(ctrl.commands[index:index + 9], [b'MC1\r\n', b'VF\r\n', b'AU0\r\n',
                                                         b'RF0005970000\r\n', b'MD2\r\n', b'ST005000\r\n',
                                                         b'AT1\r\n', b'MC0\r\n', b'RX\r\n'])
        self.assertIsNotNone(ctrl.pending_vfo_target)
        ctrl.set_vfo_text('VF RF0005970000 ST005000 AU0 MD2 AT1', 2)
        self.assertIsNone(ctrl.pending_vfo_target)

    def test_stop_during_paced_configuration_cancels_remaining_writes_and_restores(self):
        ctrl, scan = prepare()
        stale = scan.pace_timer.callback
        ctrl.on_operation_stop(None)
        before = len(ctrl.commands)
        stale()
        self.assertEqual(len(ctrl.commands), before)
        complete_restore(scan)
        self.assertIsNone(ctrl.temporary_scan)
        self.assertFalse(any(c.startswith(b'SS') for c in ctrl.commands))

    def test_restoration_rejection_retains_snapshot_and_does_not_apply_pending_target(self):
        ctrl, scan = prepare()
        activate(ctrl, scan)
        after = Mock()
        ctrl.restore_temporary_scan(after)
        scan.pace_timer.Fire()  # GS2
        scan.pace_timer.Fire()  # BS%%
        scan.receive('?')
        self.assertEqual(scan.phase, 'restore_failed')
        self.assertIs(ctrl.temporary_scan, scan)
        self.assertIsNotNone(scan.saved)
        after.assert_not_called()
        self.assertIn('Scanner rejected BS%% while restoring temporary Search Group 2.', ctrl.errors[-1])


if __name__ == '__main__':
    unittest.main()
