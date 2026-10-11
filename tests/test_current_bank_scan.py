"""Focused offline tests; never open a serial port or write to hardware."""
import importlib.machinery
import importlib.util
import ast
import re
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace, MethodType
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from current_bank_scan import CurrentBankScan, BANK_ORDER, group_snapshot

loader = importlib.machinery.SourceFileLoader('aor_scan_test_main', str(ROOT / 'aor_ctrl_main.pyw'))
spec = importlib.util.spec_from_loader(loader.name, loader)
main = importlib.util.module_from_spec(spec)
loader.exec_module(main)


class Timer:
    def Stop(self):
        pass


def header(group, level=9, dwell=60):
    return 'GM%d XD2.0 XB %03d XA 023 XP%02d XM2' % (group, level, dwell)


def membership(members):
    return 'BM ' + ''.join(bank if bank in members else '-' for bank in 'ABCDEFGHIJabcdefghij')


class Controller:
    def __init__(self):
        self.commands = []
        self.errors = []
        self.memory_banks = {bank: {'bank': bank, 'channels': 10, 'name': ''} for bank in BANK_ORDER}
        self.receiver_status = {'context': 'VFO-A'}
        self.serial = SimpleNamespace(is_open=True)
        self.aor_status = Mock()
        self.cbx_scan = Mock()
        self.scan_levels = {}
        self.current_scan_level = None
        self.level_squelch_value = 12
        self.group_response = {'scan': None}
        self.temporary_scan = None
        self.active_operation = None
        self.level_squelch_pending = False
        self.level_squelch_requested = None
        self.level_in_flight = None
        self.level_debounce_ready = False
        self.level_readback_timer = None
        self.level_squelch_timer = Mock()
        self.clear_level_edits = MethodType(main.AorCtrl.clear_level_edits, self)
        self.fail_level_edit = MethodType(main.AorCtrl.fail_level_edit, self)
        self.scan_call_later = lambda milliseconds, callback: Timer()
        self.update_connection_ui = Mock()
        self.update_operation_ui = Mock()
        self.refresh_level_context = Mock()
        self.enter_vfo_level_context = Mock()
        self.show_now_receiving = Mock()

    def normal_vfo_command(self):
        return b'VA\r\n'

    def write_serial(self, command):
        self.commands.append(command)
        return len(command)

    def report_scan_error(self, message):
        self.errors.append(message)

    def parse_memory_line(self, text):
        return main.AorCtrl.parse_memory_line(self, text)

    def validate_fields(self, fields, prefixes):
        return main.AorCtrl.validate_fields(self, fields, prefixes)


class ScanTests(unittest.TestCase):
    def create(self, level=31, ready=None):
        controller = Controller()
        scan = CurrentBankScan(controller, controller.memory_banks['J'], level, ready)
        controller.temporary_scan = scan
        scan.start()
        return controller, scan

    def feed_bank(self, scan, populated=False):
        bank = scan.helper
        for n in range(10):
            suffix = ('MP0 RF0093500000 ST100000 AU0 MD0 AT0 TMStation'
                      if populated and n == 0 else '---')
            scan.receive('MX%s%02d %s' % (bank, n, suffix))

    def feed_group(self, scan, group, members, level=9, dwell=60):
        scan.receive(header(group, level, dwell))
        scan.receive(membership(members))

    def prepare(self, level=31, ready=None, dwell=60):
        controller, scan = self.create(level, ready)
        self.feed_bank(scan)  # A is proved empty, with ten explicit empty slots.
        self.feed_group(scan, 4, 'BCD', dwell=dwell)
        self.feed_group(scan, 1, 'B', dwell=dwell)
        self.feed_group(scan, 2, '', dwell=dwell)
        self.feed_group(scan, 2, 'AJ', level=scan.level, dwell=dwell)
        scan.receive('MS MXJ02 MP0 RF0446500000 ST012500 AU0 MD1 AT0 TMPMR')
        return controller, scan

    def test_blank_name_is_not_evidence_and_all_slots_required(self):
        controller, scan = self.create()
        self.feed_bank(scan, populated=True)  # A is unnamed but populated.
        self.assertEqual(scan.helper, 'a')
        for n in range(9):
            scan.receive('MXa%02d ---' % n)
        self.assertEqual(scan.phase, 'empty')
        self.assertFalse(any(b'GM' in command for command in controller.commands))
        scan.receive('MXa09 ---')
        self.assertEqual(scan.phase, 'original')
        self.assertEqual(scan.helper, 'a')

    def test_refuse_when_no_verified_empty_bank(self):
        controller, scan = self.create()
        while scan.phase == 'empty':
            self.feed_bank(scan, populated=True)
        self.assertIsNone(controller.temporary_scan)
        self.assertTrue(controller.errors)
        self.assertFalse(any(b'GM' in c or b'BM' in c or b'MS' in c for c in controller.commands))

    def test_incomplete_or_question_mark_does_not_prove_empty(self):
        for response in ('?', 'MXA00 --'):
            controller, scan = self.create()
            scan.receive(response)
            self.assertIsNone(controller.temporary_scan)
            self.assertFalse(any(b'BM' in c for c in controller.commands))

    def test_empty_group_preferred_and_start_sequence(self):
        controller, scan = self.prepare()
        self.assertEqual(scan.group, 2)  # group 1 was occupied, group 2 unused.
        self.assertEqual(scan.phase, 'active')
        self.assertIn(b'GM2\r\nBM%%AJ\r\nXB031\r\nGM\r\n', controller.commands)
        self.assertEqual(controller.commands[-1], b'GM2\r\nMSJ\r\nRX\r\n')
        self.assertEqual(controller.active_operation, ('Memory Scan', 'Current Bank'))
        self.assertFalse(any(b'GM0' in c for c in controller.commands))

    def test_no_free_group_falls_back_to_fully_saved_group_one(self):
        controller, scan = self.create()
        self.feed_bank(scan)
        self.feed_group(scan, 4, 'BCD')
        for group in range(1, 10):
            self.feed_group(scan, group, 'B')
        self.assertEqual(scan.group, 1)
        self.assertEqual(scan.saved['members'], ('B',))
        self.assertEqual(set(scan.saved['raw']), {'XA', 'XB', 'XD', 'XM', 'XP'})

    def test_confirmed_listening_db_replaces_borrowed_group_level(self):
        controller, scan = self.prepare(level=None)
        self.assertEqual(scan.level, 12)
        self.assertEqual(controller.current_scan_level, 12)
        self.assertEqual(controller.scan_levels[2], 12)
        self.assertIn(b'GM2\r\nBM%%AJ\r\nXB012\r\nGM\r\n', controller.commands)
        scan.restore()
        self.feed_group(scan, 2, '')
        self.feed_group(scan, 4, 'BCD')
        self.assertEqual(controller.level_squelch_value, 12)
        self.assertIsNone(controller.temporary_scan)
        self.assertFalse(any(re.match(rb'DB[0-9]', command) for command in controller.commands))

    def test_replacement_echo_waits_for_complete_gm_readback(self):
        controller, scan = self.create()
        self.feed_bank(scan)
        self.feed_group(scan, 4, 'BCD')
        self.feed_group(scan, 1, '')
        scan.receive(membership('AJ'))  # BM write acknowledgement, before GM
        scan.receive('XB031')
        self.assertEqual(scan.phase, 'configured')
        self.assertFalse(any(b'MSJ' in c for c in controller.commands))
        self.feed_group(scan, 1, 'AJ', level=31)
        self.assertEqual(scan.phase, 'starting')
        scan.receive('MS MXJ02 MP0 RF0446500000 ST012500 AU0 MD1 AT0 TMPMR')
        self.assertEqual(scan.phase, 'active')
        scan.restore()
        scan.receive(membership(''))
        scan.receive('XB009')
        self.assertEqual(scan.phase, 'restoring')
        self.feed_group(scan, 1, '')
        self.feed_group(scan, 4, 'BCD')
        self.assertIsNone(controller.temporary_scan)

    def test_restore_every_parameter_and_previous_group_before_callback(self):
        controller, scan = self.prepare(dwell=99)
        done = Mock()
        scan.restore(done)
        self.assertEqual(controller.commands[-1],
                         b'VA\r\nGM2\r\nBM%%\r\nXA023\r\nXB009\r\nXD20\r\nXM2\r\nXP99\r\nGM\r\n')
        done.assert_not_called()
        self.feed_group(scan, 2, '', dwell=99)
        self.assertEqual(controller.commands[-1], b'GM4\r\nGM\r\n')
        done.assert_not_called()
        self.feed_group(scan, 4, 'BCD', dwell=99)
        done.assert_called_once()
        self.assertIsNone(controller.temporary_scan)
        self.assertEqual(controller.commands[-1], b'RX\r\n')

    def test_mismatch_aborts_start_and_restores_modified_group(self):
        controller, scan = self.create()
        self.feed_bank(scan)
        self.feed_group(scan, 4, 'BCD')
        self.feed_group(scan, 1, '')
        self.feed_group(scan, 1, 'A', level=31)  # missing target J
        self.assertEqual(scan.phase, 'restoring')
        self.assertFalse(any(b'MSJ' in c for c in controller.commands))
        self.feed_group(scan, 1, '')
        self.feed_group(scan, 4, 'BCD')
        self.assertTrue(controller.errors)
        self.assertIsNone(controller.temporary_scan)

    def test_rejected_scan_start_restores_instead_of_claiming_success(self):
        controller, scan = self.create()
        self.feed_bank(scan)
        self.feed_group(scan, 4, 'BCD')
        self.feed_group(scan, 1, '')
        self.feed_group(scan, 1, 'AJ', level=31)
        self.assertEqual(scan.phase, 'starting')
        scan.receive('?')
        self.assertEqual(scan.phase, 'restoring')
        self.assertIsNone(controller.active_operation)
        self.feed_group(scan, 1, '')
        self.feed_group(scan, 4, 'BCD')
        self.assertIsNone(controller.temporary_scan)
        self.assertTrue(controller.errors)

    def test_restore_mismatch_keeps_original_snapshot_and_no_automatic_retry(self):
        controller, scan = self.prepare()
        scan.restore()
        self.feed_group(scan, 2, 'B')
        self.assertEqual(scan.phase, 'restore_failed')
        self.assertEqual(scan.saved['members'], ())
        before = len(controller.commands)
        scan.timeout()
        self.assertEqual(len(controller.commands), before)
        self.assertTrue(controller.errors)

    def test_failed_membership_write_attempts_restore_once(self):
        controller, scan = self.create()
        self.feed_bank(scan)
        self.feed_group(scan, 4, 'BCD')
        normal_write = controller.write_serial
        controller.write_serial = lambda command: (0 if b'BM%%AJ' in command else normal_write(command))
        self.feed_group(scan, 1, '')
        self.assertEqual(scan.phase, 'restoring')
        self.assertTrue(scan.modified)
        self.assertFalse(any(b'MSJ' in c for c in controller.commands))
        self.feed_group(scan, 1, '')
        self.feed_group(scan, 4, 'BCD')
        self.assertIsNone(controller.temporary_scan)
        self.assertEqual(sum(b'BM%%' in command for command in controller.commands), 1)

    def test_read_timeout_before_probing_groups_leaves_groups_unchanged(self):
        controller, scan = self.create()
        scan.receive('MXA00 ---')
        scan.timeout()
        self.assertIsNone(controller.temporary_scan)
        self.assertFalse(any(b'GM' in c or b'BM' in c or b'MS' in c for c in controller.commands))
        self.assertIn('timed out', controller.errors[0])

    def test_incomplete_settings_never_enable_membership_write(self):
        controller, scan = self.create()
        self.feed_bank(scan)
        scan.receive('GM4 XB009 XA023 XD2.0 XP00')  # XM absent
        scan.receive(membership('BCD'))
        self.assertFalse(any(b'BM%%' in c for c in controller.commands))
        self.assertTrue(controller.errors)

    def test_multiblock_empty_bank_requires_all_blocks(self):
        controller = Controller()
        controller.memory_banks['A']['channels'] = 23
        scan = CurrentBankScan(controller, controller.memory_banks['J'])
        controller.temporary_scan = scan
        scan.start()
        self.assertEqual(controller.commands, [b'MAA\r\n'])
        for n in range(29):
            scan.receive('MXA%02d ---' % n)
        self.assertEqual(scan.phase, 'empty')
        self.assertEqual(controller.commands, [b'MAA\r\n', b'MA\r\n', b'MA\r\n'])
        scan.receive('MXA29 ---')
        self.assertEqual(scan.phase, 'original')

    def test_populated_candidate_rejected_immediately_without_more_blocks(self):
        controller = Controller()
        controller.memory_banks['A']['channels'] = 70
        scan = CurrentBankScan(controller, controller.memory_banks['J'])
        controller.temporary_scan = scan
        scan.start()
        self.assertEqual(controller.commands, [b'MAA\r\n'])
        scan.receive('MXA00 MP0 RF0093500000 ST100000 AU0 MD0 AT0 TMStation')
        self.assertEqual(scan.helper, 'a')
        self.assertEqual(controller.commands, [b'MAA\r\n', b'MAa\r\n'])
        for n in range(1, 10):  # Drain the rejected candidate's outstanding tail.
            scan.receive('MXA%02d ---' % n)
        self.assertEqual(len(scan.remaining), 10)
        self.assertEqual(scan.phase, 'empty')

    def test_empty_candidate_advances_one_block_only_after_ten_empty_slots(self):
        controller = Controller()
        controller.memory_banks['A']['channels'] = 70
        scan = CurrentBankScan(controller, controller.memory_banks['J'])
        controller.temporary_scan = scan
        scan.start()
        for n in range(9):
            scan.receive('MXA%02d ---' % n)
        scan.receive('MXA00 ---')  # Duplicate is not evidence for another slot.
        scan.receive('MXA10 ---')  # An unrequested block is not completion evidence.
        self.assertEqual(controller.commands, [b'MAA\r\n'])
        scan.receive('MXA09 ---')
        self.assertEqual(controller.commands, [b'MAA\r\n', b'MA\r\n'])
        for n in range(10, 69):
            scan.receive('MXA%02d ---' % n)
        self.assertEqual(scan.phase, 'empty')
        self.assertFalse(any(b'GM' in command for command in controller.commands))
        scan.receive('MXA69 ---')
        self.assertEqual(scan.phase, 'original')
        self.assertEqual(controller.commands[:-1], [b'MAA\r\n'] + [b'MA\r\n'] * 6)

    def test_populated_second_block_prevents_reading_third_block(self):
        controller = Controller()
        controller.memory_banks['A']['channels'] = 70
        scan = CurrentBankScan(controller, controller.memory_banks['J'])
        controller.temporary_scan = scan
        scan.start()
        for n in range(10):
            scan.receive('MXA%02d ---' % n)
        scan.receive('MXA10 MP0 RF0093500000 ST100000 AU0 MD0 AT0 TMStation')
        self.assertEqual(controller.commands, [b'MAA\r\n', b'MA\r\n', b'MAa\r\n'])
        self.assertEqual(scan.helper, 'a')

    def test_preparation_is_not_active_scan_and_stop_cancels_it(self):
        controller, scan = self.create()
        self.assertIsNone(controller.active_operation)
        self.assertIn('Preparing Memory Bank J scan', controller.aor_status.SetStatusText.call_args.args[0])
        self.assertNotIn('Scanning Memory Bank', controller.aor_status.SetStatusText.call_args.args[0])
        scan.restore()
        self.assertIsNone(controller.temporary_scan)
        self.assertIsNone(controller.active_operation)
        self.assertFalse(any(b'MS' in command for command in controller.commands))
        self.assertEqual(controller.level_squelch_value, 12)

    def test_ready_group_reverifies_db_before_ms_and_preserves_other_settings(self):
        ready = Mock()
        controller, scan = self.create(ready=ready)
        self.feed_bank(scan)
        self.feed_group(scan, 4, 'BCD')
        self.feed_group(scan, 1, '')
        self.feed_group(scan, 1, 'AJ', level=31)
        self.assertEqual(scan.phase, 'ready')
        scan.begin_scan(12)
        self.assertEqual(scan.phase, 'level_start')
        self.assertEqual(controller.commands[-1], b'XB012\r\nGM\r\n')
        self.assertFalse(any(b'MSJ' in command for command in controller.commands))
        self.feed_group(scan, 1, 'AJ', level=12, dwell=15)
        self.assertEqual(scan.phase, 'starting')
        self.assertEqual(controller.commands[-1], b'GM1\r\nMSJ\r\nRX\r\n')
        self.assertEqual(controller.scan_levels[1], 12)
        controller.aor_status.SetStatusText.assert_called_with('Scanning Memory Bank J')

    def test_unknown_listening_db_refuses_preparation_without_group_changes(self):
        controller = Controller()
        controller.level_squelch_value = None
        scan = CurrentBankScan(controller, controller.memory_banks['J'])
        controller.temporary_scan = scan
        scan.start()
        self.assertIsNone(controller.temporary_scan)
        self.assertEqual(controller.commands, [])
        self.assertIn('confirmed listening Squelch', controller.errors[0])


class GuiBoundaryTests(unittest.TestCase):
    def current_bank_start_controller(self):
        ctrl = self.level_controller()
        ctrl.level_squelch_value = 12
        ctrl.current_scan_level = 0  # Stale borrowed-group level must not win.
        ctrl.level_vfo_context = True
        ctrl.operation_selection = lambda: ('Memory Scan', 'Current Bank')
        ctrl.operation_memory_bank = lambda: ctrl.memory_banks['A']
        ctrl.memory_channel_pending = ctrl.memory_select_read = ctrl.memory_flag_edit = None
        ctrl.filling_banks = False
        ctrl.group_dialogs = {'scan': None, 'search': None}
        ctrl.bandscope = None
        ctrl.prepare_current_bank_scan = MethodType(main.AorCtrl.prepare_current_bank_scan, ctrl)
        ctrl.scan_lifecycle_action = None
        ctrl.restore_temporary_scan = MethodType(main.AorCtrl.restore_temporary_scan, ctrl)
        for name in ('source_choice', 'memory_source_panel', 'scan_source_panel', 'range_source_panel',
                     'bank_source_panel', 'search_source_panel', 'cbx_search', 'bt_start', 'bt_stop',
                     'operation_choice', 'operation_panel', 'panel_1'):
            setattr(ctrl, name, Mock())
        ctrl.GetToolBar = Mock(return_value=Mock())
        ctrl.operation_settings_tool_id = 1234
        ctrl.Layout = Mock()
        ctrl.now_text = Mock()
        ctrl.memory_rows = {}
        ctrl.receiving_channel = ctrl.vfo_channel = None
        ctrl.operation_signal_open = False
        ctrl.memory_scan_paused = MethodType(main.AorCtrl.memory_scan_paused, ctrl)
        ctrl.paused_scan_target = MethodType(main.AorCtrl.paused_scan_target, ctrl)
        ctrl.finish_operation_stop = MethodType(main.AorCtrl.finish_operation_stop, ctrl)
        return ctrl

    def test_start_captures_db_not_stale_scan_level_and_stop_remains_usable(self):
        ctrl = self.current_bank_start_controller()
        main.AorCtrl.on_operation_start(ctrl, None)
        scan = ctrl.temporary_scan
        self.assertEqual(scan.level, 12)
        self.assertEqual(ctrl.commands, [b'MAa\r\n'])
        self.assertIsNone(ctrl.active_operation)
        main.AorCtrl.update_operation_ui(ctrl)
        ctrl.bt_start.Enable.assert_called_with(False)
        ctrl.bt_stop.Enable.assert_called_with(True)
        main.AorCtrl.on_operation_stop(ctrl, None)
        self.assertIsNone(ctrl.temporary_scan)
        self.assertEqual(ctrl.level_squelch_value, 12)
        ctrl.accept_operation_parameter('DB012')
        main.AorCtrl.update_operation_ui(ctrl)
        ctrl.bt_start.Enable.assert_called_with(True)

    def test_squelch_stays_db_12_then_temporary_xb_12_and_returns_to_db_12(self):
        ctrl = self.current_bank_start_controller()
        main.AorCtrl.on_operation_start(ctrl, None)
        scan = ctrl.temporary_scan
        helper = scan.helper
        for n in range(10):
            scan.receive('MX%s%02d ---' % (helper, n))
        scan.receive(header(4))
        scan.receive(membership('BCD'))
        scan.receive(header(1))
        scan.receive(membership(''))
        self.assertEqual(ctrl.commands[-1], b'GM1\r\nBM%%aA\r\nXB012\r\nGM\r\n')
        scan.receive(header(1, level=12))
        scan.receive(membership('Aa'))
        self.assertEqual(ctrl.commands[-1], b'GM1\r\nMSA\r\nRX\r\n')
        self.assertEqual(ctrl.level_context(), ('XB', 1))
        ctrl.update_level_ui()
        ctrl.sql.SetValue.assert_called_with(12)
        ctrl.sql_value.SetLabel.assert_called_with('12')
        self.assertEqual(ctrl.level_squelch_value, 12)
        with patch.object(main.wx.SystemSettings, 'GetColour', return_value='grey'):
            main.AorCtrl.show_now_receiving(ctrl)
        ctrl.now_text.SetLabel.assert_called_with('Scanning...')
        scan.receive('MS MXA53 MP0 RF0093500000 ST100000 AU0 MD0 AT0 TMStation')
        self.assertEqual(scan.phase, 'active')
        ctrl.aor_status.SetStatusText.assert_called_with('Scanning Memory Bank A')
        main.AorCtrl.update_operation_ui(ctrl)
        ctrl.bt_start.Enable.assert_called_with(False)
        ctrl.bt_stop.Enable.assert_called_with(True)
        main.AorCtrl.on_operation_stop(ctrl, None)
        self.assertEqual(ctrl.level_context(), ('DB', None))
        ctrl.update_level_ui()
        ctrl.sql_value.SetLabel.assert_called_with('12')
        scan.receive(header(1))
        scan.receive(membership(''))
        scan.receive(header(4))
        scan.receive(membership('BCD'))
        self.assertIsNone(ctrl.temporary_scan)
        self.assertEqual(ctrl.level_context(), ('DB', None))
        self.assertEqual(ctrl.level_squelch_value, 12)
        self.assertEqual(ctrl.commands[-1], b'DB\r\n')
        self.assertFalse(any(re.search(rb'DB[0-9]', command) for command in ctrl.commands))
        self.assertNotIn(unittest.mock.call(0), ctrl.sql.SetValue.call_args_list)

    def test_start_waits_for_confirmed_db_instead_of_using_unknown_or_pending_value(self):
        for value, pending in ((None, False), (12, True)):
            ctrl = self.current_bank_start_controller()
            ctrl.level_squelch_value = value
            ctrl.level_squelch_pending = pending
            main.AorCtrl.on_operation_start(ctrl, None)
            self.assertIsNone(ctrl.temporary_scan)
            self.assertEqual(ctrl.commands, [])
            self.assertIn('confirmed listening Squelch', ctrl.aor_status.SetStatusText.call_args.args[0])

    def test_zero_scan_level_is_used_only_when_listening_db_is_zero(self):
        ctrl = self.current_bank_start_controller()
        ctrl.level_squelch_value = 0
        main.AorCtrl.on_operation_start(ctrl, None)
        scan = ctrl.temporary_scan
        self.assertEqual(scan.level, 0)
        for n in range(10):
            scan.receive('MXa%02d ---' % n)
        scan.receive(header(4))
        scan.receive(membership('BCD'))
        scan.receive(header(1, level=57))
        scan.receive(membership(''))
        self.assertEqual(ctrl.commands[-1], b'GM1\r\nBM%%aA\r\nXB000\r\nGM\r\n')

    def level_controller(self):
        ctrl = Controller()
        ctrl.connected = True
        ctrl.alive = SimpleNamespace(is_set=lambda: True)
        ctrl.memory_inventory = ctrl.memory_bank_transfer = None
        ctrl.level_squelch_requested = None
        ctrl.level_squelch_value = 255
        ctrl.scan_level_context = ('DB', None)
        ctrl.level_vfo_context = False
        ctrl.level_readback_timer = None
        ctrl.level_squelch_timer = Mock()
        ctrl.settings_dialog = None
        ctrl.group_loading = {'scan': None, 'search': None}
        ctrl.sql = Mock()
        ctrl.sql_value = Mock()
        ctrl.sizer_18_staticbox = Mock()
        ctrl.cbx_scan.GetValue.return_value = '3'
        ctrl.group_response['scan'] = 3  # Explicitly prepared/selected scanner group.
        ctrl.operation_selection = lambda: ('Frequency Search', 'Range')
        for method in ('level_context', 'displayed_level', 'update_level_ui', 'refresh_level_context',
                       'enter_vfo_level_context', 'request_group', 'accept_operation_parameter',
                       'on_level_readback_timeout'):
            setattr(ctrl, method, MethodType(getattr(main.AorCtrl, method), ctrl))
        return ctrl

    def test_db_to_xb_context_queries_without_threshold_write(self):
        ctrl = self.level_controller()
        ctrl.sql.SetValue(255)
        ctrl.operation_selection = lambda: ('Memory Scan', 'Scan Group')
        ctrl.refresh_level_context()
        self.assertEqual(ctrl.commands, [b'GM3\r\nGM\r\n'])

        ctrl.sizer_18_staticbox.SetLabel.assert_called_with('Squelch')
        ctrl.sql.SetToolTip.assert_called_with('Signal threshold for opening the squelch or stopping the scan. 0 disables it.')
        self.assertNotIn(unittest.mock.call(0), ctrl.sql.SetValue.call_args_list)
        ctrl.sql_value.SetLabel.assert_called_with('---')
        ctrl.sql.Enable.assert_called_with(False)
        ctrl.group_response['scan'] = 3
        ctrl.group_loading['scan'] = None
        ctrl.accept_operation_parameter('XB023')
        ctrl.sql.SetValue.assert_called_with(23)
        ctrl.sql_value.SetLabel.assert_called_with('23')
        self.assertEqual(ctrl.level_squelch_value, 255)
        self.assertEqual(ctrl.commands, [b'GM3\r\nGM\r\n'])

    def test_context_change_cancels_old_slider_debounce(self):
        ctrl = self.level_controller()
        ctrl.level_squelch_requested = 255
        ctrl.operation_selection = lambda: ('Memory Scan', 'Scan Group')
        ctrl.refresh_level_context()
        ctrl.level_squelch_timer.Stop.assert_called()
        self.assertIsNone(ctrl.level_squelch_requested)
        main.AorCtrl.on_level_squelch_timer(ctrl, None)
        self.assertEqual(ctrl.commands, [b'GM3\r\nGM\r\n'])

    def test_xb_to_db_queries_and_waits_for_current_scanner_value(self):
        ctrl = self.level_controller()
        ctrl.scan_level_context = ('XB', 3)
        ctrl.scan_levels[3] = 23
        ctrl.sql.SetValue(23)
        ctrl.refresh_level_context()
        self.assertEqual(ctrl.commands, [b'DB\r\n'])
        ctrl.sizer_18_staticbox.SetLabel.assert_called_with('Squelch')
        ctrl.sql.SetToolTip.assert_called_with('Signal threshold for opening the squelch or stopping the scan. 0 disables it.')
        ctrl.sql_value.SetLabel.assert_called_with('---')
        self.assertNotIn(unittest.mock.call(0), ctrl.sql.SetValue.call_args_list)
        ctrl.sql.Enable.assert_called_with(False)
        ctrl.accept_operation_parameter('DB 255')
        ctrl.sql.SetValue.assert_called_with(255)
        ctrl.sql.Enable.assert_called_with(True)
        self.assertEqual(ctrl.scan_levels[3], 23)
        self.assertEqual(ctrl.commands, [b'DB\r\n'])

    def test_temporary_group_refresh_queries_xb_only(self):
        ctrl = self.level_controller()
        ctrl.operation_selection = lambda: ('Memory Scan', 'Current Bank')
        ctrl.temporary_scan = SimpleNamespace(group=2, busy=False)
        ctrl.group_response['scan'] = 2
        ctrl.scan_levels[2] = 14
        ctrl.refresh_level_context()
        self.assertEqual(ctrl.commands, [b'XB\r\n'])
        ctrl.accept_operation_parameter('XB037')
        ctrl.sql.SetValue.assert_called_with(37)
        self.assertEqual(ctrl.current_scan_level, 37)

    def test_vfo_context_overrides_scan_selection_until_source_change(self):
        ctrl = self.level_controller()
        ctrl.operation_selection = lambda: ('Memory Scan', 'Scan Group')
        ctrl.enter_vfo_level_context()
        self.assertEqual(ctrl.level_context(), ('DB', None))
        self.assertEqual(ctrl.commands, [b'DB\r\n'])
        ctrl.accept_operation_parameter('DB000')
        ctrl.sql_value.SetLabel.assert_called_with('Off')
        ctrl.level_vfo_context = False
        self.assertEqual(ctrl.level_context(), ('XB', 3))

    def test_db_slider_writes_and_reads_db_only(self):
        ctrl = self.level_controller()
        ctrl.level_squelch_requested = 57
        main.AorCtrl.on_level_squelch_timer(ctrl, None)
        self.assertEqual(ctrl.commands, [b'DB057\r\nDB\r\n'])
        self.assertEqual(ctrl.level_squelch_value, 255)
        ctrl.accept_operation_parameter('DB057')
        self.assertEqual(ctrl.level_squelch_value, 57)

    def test_one_main_slider_and_no_temporary_diagnostic_ui(self):
        frame = (ROOT / 'aor_control_frame.py').read_text(encoding='utf-8')
        main_source = (ROOT / 'aor_ctrl_main.pyw').read_text(encoding='utf-8')
        sliders = [node for node in ast.walk(ast.parse(frame)) if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute) and node.func.attr == 'Slider']
        self.assertEqual(len(sliders), 1)
        self.assertIn('wx.StaticBox(self.panel_1, wx.ID_ANY, "Squelch")', frame)
        self.assertNotIn('VFO squelch read-back:', main_source)
        self.assertNotIn('squelch_diagnostic_text', main_source)

    def test_idle_current_bank_uses_db_until_temporary_group_exists(self):
        ctrl = self.level_controller()
        ctrl.current_scan_level = 255
        ctrl.operation_selection = lambda: ('Memory Scan', 'Current Bank')
        ctrl.refresh_level_context()
        ctrl.sql_value.SetLabel.assert_called_with('---')
        ctrl.sql.SetValue.assert_not_called()
        ctrl.sql.Enable.assert_called_with(False)
        ctrl.sizer_18_staticbox.SetLabel.assert_called_with('Squelch')
        main.AorCtrl.on_level_squelch_change(ctrl, None)
        self.assertIsNone(ctrl.level_squelch_requested)
        self.assertEqual(ctrl.commands, [b'DB\r\n'])
        ctrl.accept_operation_parameter('DB057')
        ctrl.sql.SetValue.assert_called_with(57)
        ctrl.sql.Enable.assert_called_with(True)
        self.assertEqual(ctrl.level_context(), ('DB', None))

    def test_contextual_slider_label_value_and_group_zero_lock(self):
        ctrl = Controller()
        ctrl.connected = True
        ctrl.alive = SimpleNamespace(is_set=lambda: True)
        ctrl.memory_inventory = ctrl.memory_bank_transfer = None
        ctrl.level_squelch_requested = None
        ctrl.level_squelch_value = 57
        ctrl.group_loading = {'scan': None}
        ctrl.sql = Mock()
        ctrl.sql_value = Mock()
        ctrl.sizer_18_staticbox = Mock()
        ctrl.level_context = lambda: main.AorCtrl.level_context(ctrl)
        ctrl.displayed_level = lambda: main.AorCtrl.displayed_level(ctrl)
        ctrl.operation_selection = lambda: ('Memory Scan', 'Scan Group')
        ctrl.cbx_scan.GetValue.return_value = '0'
        ctrl.group_response['scan'] = 0
        ctrl.scan_levels[0] = 0
        main.AorCtrl.update_level_ui(ctrl)
        ctrl.sizer_18_staticbox.SetLabel.assert_called_with('Squelch')
        ctrl.sql.Enable.assert_called_with(False)
        ctrl.sql_value.SetLabel.assert_called_with('Off')
        ctrl.cbx_scan.GetValue.return_value = '3'
        ctrl.group_response['scan'] = 3
        ctrl.scan_levels[3] = 23
        main.AorCtrl.update_level_ui(ctrl)
        ctrl.sql.Enable.assert_called_with(True)
        ctrl.sql.SetValue.assert_called_with(23)
        ctrl.operation_selection = lambda: ('Frequency Search', 'Range')
        main.AorCtrl.update_level_ui(ctrl)
        ctrl.sizer_18_staticbox.SetLabel.assert_called_with('Squelch')
        ctrl.sql.SetValue.assert_called_with(57)

    def smooth_level_controller(self):
        ctrl = self.level_controller()
        ctrl.level_squelch_value = 100
        thumb = [100]
        ctrl.sql.GetValue.side_effect = lambda: thumb[0]
        ctrl.sql.SetValue.side_effect = lambda value: thumb.__setitem__(0, value)
        for method in ('on_level_squelch_change', 'on_level_squelch_timer'):
            setattr(ctrl, method, MethodType(getattr(main.AorCtrl, method), ctrl))
        ctrl.update_connection_ui = lambda: ctrl.update_level_ui()
        def move(value):
            thumb[0] = value  # Native mouse/key movement, before EVT_SLIDER.
            ctrl.on_level_squelch_change(None)
        return ctrl, thumb, move

    def test_squelch_thumb_and_number_follow_desire_during_write(self):
        ctrl, thumb, move = self.smooth_level_controller()
        move(101)
        ctrl.sql_value.SetLabel.assert_called_with('101')
        ctrl.level_squelch_timer.StartOnce.assert_called_with(250)
        ctrl.on_level_squelch_timer(None)
        self.assertEqual(ctrl.commands, [b'DB101\r\nDB\r\n'])
        self.assertEqual(ctrl.level_squelch_value, 100)
        self.assertEqual(ctrl.level_in_flight['value'], 101)
        self.assertEqual(ctrl.level_squelch_requested, 101)
        self.assertEqual(thumb[0], 101)
        ctrl.sql.SetValue.assert_not_called()
        ctrl.sql.Enable.assert_called_with(True)
        ctrl.update_level_ui()
        ctrl.sql.SetValue.assert_not_called()
        ctrl.accept_operation_parameter('DB101')
        self.assertIsNone(ctrl.level_in_flight)
        self.assertIsNone(ctrl.level_squelch_requested)
        self.assertEqual(ctrl.level_squelch_value, 101)
        ctrl.sql.SetValue.assert_not_called()

    def test_squelch_pending_changes_coalesce_to_latest_only(self):
        ctrl, thumb, move = self.smooth_level_controller()
        move(101)
        ctrl.on_level_squelch_timer(None)
        move(102)
        move(103)
        ctrl.on_level_squelch_timer(None)  # Debounce elapsed, first read still pending.
        self.assertEqual(ctrl.commands, [b'DB101\r\nDB\r\n'])
        self.assertEqual(ctrl.level_in_flight['value'], 101)
        ctrl.sql_value.SetLabel.assert_called_with('103')
        ctrl.accept_operation_parameter('DB101')
        self.assertEqual(ctrl.commands, [b'DB101\r\nDB\r\n', b'DB103\r\nDB\r\n'])
        self.assertEqual(ctrl.level_in_flight['value'], 103)
        self.assertEqual(thumb[0], 103)
        ctrl.accept_operation_parameter('DB103')
        self.assertFalse(ctrl.level_squelch_pending)
        self.assertEqual(ctrl.level_squelch_value, 103)
        ctrl.sql.SetValue.assert_not_called()

    def test_squelch_newer_edit_keeps_its_remaining_debounce(self):
        ctrl, thumb, move = self.smooth_level_controller()
        move(101)
        ctrl.on_level_squelch_timer(None)
        move(103)
        ctrl.accept_operation_parameter('DB101')
        self.assertEqual(len(ctrl.commands), 1)
        self.assertEqual(thumb[0], 103)
        ctrl.on_level_squelch_timer(None)
        self.assertEqual(ctrl.commands[-1], b'DB103\r\nDB\r\n')

    def test_squelch_other_readback_during_debounce_does_not_cancel_desire(self):
        ctrl, thumb, move = self.smooth_level_controller()
        move(101)
        ctrl.accept_operation_parameter('DB100')
        self.assertEqual(ctrl.level_squelch_requested, 101)
        self.assertEqual(thumb[0], 101)
        ctrl.sql.SetValue.assert_not_called()
        self.assertEqual(ctrl.commands, [])
        ctrl.on_level_squelch_timer(None)
        self.assertEqual(ctrl.commands, [b'DB101\r\nDB\r\n'])

    def test_squelch_scanner_mismatch_moves_once_without_newer_edit(self):
        ctrl, thumb, move = self.smooth_level_controller()
        move(101)
        ctrl.on_level_squelch_timer(None)
        ctrl.accept_operation_parameter('DB099')
        self.assertEqual(thumb[0], 99)
        ctrl.sql.SetValue.assert_called_once_with(99)
        ctrl.update_level_ui()
        ctrl.sql.SetValue.assert_called_once_with(99)
        self.assertEqual(len(ctrl.commands), 1)

    def test_squelch_timeout_clears_queue_and_restores_confirmed_once(self):
        ctrl, thumb, move = self.smooth_level_controller()
        move(101)
        ctrl.on_level_squelch_timer(None)
        move(103)
        ctrl.on_level_squelch_timer(None)
        ctrl.on_level_readback_timeout()
        self.assertEqual(thumb[0], 100)
        ctrl.sql.SetValue.assert_called_once_with(100)
        self.assertIsNone(ctrl.level_squelch_requested)
        self.assertIsNone(ctrl.level_in_flight)
        self.assertFalse(ctrl.level_squelch_pending)
        self.assertTrue(ctrl.errors)
        ctrl.on_level_readback_timeout()
        ctrl.on_level_squelch_timer(None)
        self.assertEqual(len(ctrl.commands), 1)
        ctrl.sql.SetValue.assert_called_once_with(100)

    def test_squelch_failed_write_restores_confirmed_without_retry(self):
        ctrl, thumb, move = self.smooth_level_controller()
        ctrl.write_serial = lambda data: 0
        move(101)
        ctrl.on_level_squelch_timer(None)
        self.assertEqual(thumb[0], 100)
        ctrl.sql.SetValue.assert_called_once_with(100)
        self.assertIsNone(ctrl.level_in_flight)
        self.assertIsNone(ctrl.level_squelch_requested)
        self.assertTrue(ctrl.errors)

    def test_squelch_rejected_or_malformed_read_clears_edits(self):
        for response in ('?', 'DB999'):
            ctrl, thumb, move = self.smooth_level_controller()
            ctrl.memory_inventory = ctrl.memory_bank_transfer = None
            ctrl.bandscope = None
            ctrl.monitor_muted = False
            ctrl.mirror_console_line = Mock()
            ctrl.maybe_load_initial_bank = Mock()
            ctrl.memory_select_read = None
            move(101)
            ctrl.on_level_squelch_timer(None)
            with self.subTest(response=response), unittest.mock.patch('sys.stderr'):
                main.AorCtrl.on_serial_read(ctrl, SimpleNamespace(data=[response]))
            self.assertEqual(thumb[0], 100)
            ctrl.sql.SetValue.assert_called_once_with(100)
            self.assertIsNone(ctrl.level_in_flight)
            self.assertIsNone(ctrl.level_squelch_requested)
            self.assertEqual(len(ctrl.commands), 1)

    def test_squelch_context_switch_keeps_thumb_and_ignores_late_old_read(self):
        ctrl, thumb, move = self.smooth_level_controller()
        move(101)
        ctrl.on_level_squelch_timer(None)
        move(102)
        ctrl.operation_selection = lambda: ('Memory Scan', 'Scan Group')
        ctrl.refresh_level_context()
        self.assertEqual(ctrl.commands[-1], b'GM3\r\nGM\r\n')
        self.assertEqual(thumb[0], 102)
        ctrl.sql.SetValue.assert_not_called()
        ctrl.sql_value.SetLabel.assert_called_with('---')
        ctrl.sql.Enable.assert_called_with(False)
        self.assertIsNone(ctrl.level_squelch_requested)
        self.assertIsNone(ctrl.level_in_flight)
        ctrl.accept_operation_parameter('DB101')
        self.assertEqual(ctrl.level_squelch_value, 101)  # Old cache can update.
        self.assertEqual(thumb[0], 102)
        ctrl.sql.SetValue.assert_not_called()
        ctrl.group_response['scan'] = 3
        ctrl.group_loading['scan'] = None
        ctrl.accept_operation_parameter('XB023')
        self.assertEqual(thumb[0], 23)
        ctrl.sql.SetValue.assert_called_once_with(23)
        self.assertEqual(len(ctrl.commands), 2)  # Original DB write + new context query.

    def test_squelch_xb_edits_use_same_coalescing_path(self):
        ctrl, thumb, move = self.smooth_level_controller()
        ctrl.operation_selection = lambda: ('Memory Scan', 'Current Bank')
        ctrl.temporary_scan = SimpleNamespace(group=2, busy=False)
        ctrl.scan_level_context = ('XB', 2)
        ctrl.group_response['scan'] = 2
        ctrl.scan_levels[2] = 23
        thumb[0] = 23
        move(24)
        ctrl.on_level_squelch_timer(None)
        move(26)
        ctrl.on_level_squelch_timer(None)
        ctrl.accept_operation_parameter('XB024')
        ctrl.accept_operation_parameter('XB026')
        self.assertEqual(ctrl.commands, [b'XB024\r\nXB\r\n', b'XB026\r\nXB\r\n'])
        self.assertEqual(ctrl.current_scan_level, 26)
        self.assertEqual(ctrl.level_squelch_value, 100)
        ctrl.sql.SetValue.assert_not_called()

    def test_db_and_xb_are_distinct_and_scanner_authoritative(self):
        ctrl = Controller()
        ctrl.operation_selection = lambda: ('Frequency Search', 'Range')
        ctrl.level_squelch_value = 12
        ctrl.level_readback_timer = None
        ctrl.update_level_ui = Mock()
        ctrl.settings_dialog = None
        ctrl.level_context = lambda: main.AorCtrl.level_context(ctrl)
        main.AorCtrl.accept_operation_parameter(ctrl, 'DB057')
        self.assertEqual(ctrl.level_squelch_value, 57)
        ctrl.operation_selection = lambda: ('Memory Scan', 'Scan Group')
        ctrl.cbx_scan.GetValue.return_value = '3'
        ctrl.group_response['scan'] = 3
        ctrl.level_squelch_pending = True
        main.AorCtrl.accept_operation_parameter(ctrl, 'XB023')
        self.assertEqual(ctrl.scan_levels[3], 23)
        self.assertEqual(ctrl.level_squelch_value, 57)
        self.assertEqual(main.AorCtrl.displayed_level(ctrl), 23)
        self.assertFalse(ctrl.level_squelch_pending)
        main.AorCtrl.accept_operation_parameter(ctrl, 'DB011')
        self.assertEqual(main.AorCtrl.displayed_level(ctrl), 23)

    def test_current_bank_live_level_writes_xb_not_db(self):
        ctrl = Controller()
        ctrl.connected = True
        ctrl.operation_selection = lambda: ('Memory Scan', 'Current Bank')
        ctrl.temporary_scan = SimpleNamespace(group=2, busy=False)
        ctrl.group_response['scan'] = 2
        ctrl.scan_levels[2] = 23
        ctrl.level_context = lambda: main.AorCtrl.level_context(ctrl)
        ctrl.displayed_level = lambda: main.AorCtrl.displayed_level(ctrl)
        ctrl.level_squelch_requested = 37
        ctrl.sql = Mock()
        ctrl.on_level_readback_timeout = Mock()
        main.AorCtrl.on_level_squelch_timer(ctrl, None)
        self.assertEqual(ctrl.commands[-1], b'XB037\r\nXB\r\n')
        self.assertEqual(ctrl.scan_levels[2], 23)  # unchanged until read-back
        ctrl.level_readback_timer.Stop()

    def test_ms_and_sm_display_frequency_without_touching_vfo_cache(self):
        ctrl = Controller()
        ctrl.monitor_muted = False
        ctrl.vfo_channel = None
        ctrl.operation_signal_open = False
        ctrl.receiver_status = None
        ctrl.clear_memory_import_error = Mock()
        ctrl.update_receiver_switch_ui = Mock()
        ctrl.rb_vfos = Mock()
        ctrl.rb_vfos.GetSelection.return_value = 1
        ctrl.lb_vfa, ctrl.lb_vfb, ctrl.lb_vfo = Mock(), Mock(), Mock()
        ctrl.vfa, ctrl.vfb, ctrl.vfo = '0093500000', '0145500000', '0433500000'
        ctrl.vfo_status = {0: 'cached VA', 1: 'cached VB', 2: 'cached VF'}
        ctrl.show_scan_frequency = lambda hz: main.AorCtrl.show_scan_frequency(ctrl, hz)
        for prefix in ('MS', 'SM'):
            main.AorCtrl.receive_context_status(ctrl,
                prefix + ' MXJ02 MP0 RF0446500000 ST012500 AU0 MD1 AT0 TMPMR')
            ctrl.lb_vfb.SetLabel.assert_called_with(main.display_vfo_frequency(446500000))
        self.assertEqual((ctrl.vfa, ctrl.vfb, ctrl.vfo), ('0093500000', '0145500000', '0433500000'))
        self.assertEqual(ctrl.vfo_status, {0: 'cached VA', 1: 'cached VB', 2: 'cached VF'})
        ctrl.cbx_step = Mock()
        ctrl.cbx_step.GetItems.return_value = ['12.5', '100']
        ctrl.ckbx_auto, ctrl.cbx_mode, ctrl.ckbx_att = Mock(), Mock(), Mock()
        main.AorCtrl.set_vfo_text(ctrl, 'VB RF0145500000 ST012500 AU0 MD1 AT0', 1)
        ctrl.lb_vfb.SetLabel.assert_called_with(main.display_vfo_frequency(145500000))

    def test_lifecycle_actions_wait_for_verified_restoration(self):
        ctrl = Controller()
        ctrl.scan_lifecycle_action = None
        ctrl.temporary_scan = Mock()
        after = Mock()
        main.AorCtrl.restore_temporary_scan(ctrl, after)
        after.assert_not_called()
        completed = ctrl.temporary_scan.restore.call_args.args[0]
        ctrl.temporary_scan = None
        completed()
        after.assert_called_once()

    def test_disconnected_transport_retains_snapshot_without_trapping_close(self):
        ctrl = Controller()
        ctrl.scan_lifecycle_action = None
        ctrl.temporary_scan = SimpleNamespace(phase='restore_failed', saved={'original': 'snapshot'}, restore=Mock())
        original = ctrl.temporary_scan
        after = Mock()
        main.AorCtrl.restore_temporary_scan(ctrl, after, disconnecting=True)
        after.assert_not_called()
        original.restore.call_args.args[0]()
        after.assert_called_once()
        self.assertIsNone(ctrl.temporary_scan)
        self.assertIs(ctrl.scan_recovery, original)
        self.assertTrue(ctrl.errors)

    def test_close_supersedes_pending_action_without_repeating_restore(self):
        ctrl = Controller()
        ctrl.scan_lifecycle_action = None
        ctrl.temporary_scan = SimpleNamespace(phase='restoring', restore=Mock(), finish_callback=None)
        first, close = Mock(), Mock()
        main.AorCtrl.restore_temporary_scan(ctrl, first)
        main.AorCtrl.restore_temporary_scan(ctrl, close, disconnecting=True)
        scan = ctrl.temporary_scan
        scan.restore.assert_called_once()
        ctrl.temporary_scan = None
        scan.finish_callback()
        first.assert_not_called()
        close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
