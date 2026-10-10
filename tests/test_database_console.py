"""Targeted DATABASE/audio/console checks with no scanner or serial-port opens."""
from contextlib import redirect_stderr
from decimal import Decimal
import io
import threading
from types import SimpleNamespace, MethodType
import unittest
from unittest.mock import Mock, patch

from test_current_bank_scan import Controller, main
from command_console import command_bytes, send_command, ConsoleHistory, CommandConsole


STATUS = 'VA RF0005970000 ST005000 AU0 MD2 AT0'
RECORD = SimpleNamespace(frequency_hz=Decimal('5970000'), step_hz=Decimal('5000'),
                         values={'Station': 'BBC World Service', 'Mode': 'AM'})


def controller():
    ctrl = Controller()
    ctrl.connected = True
    ctrl.alive = threading.Event()
    ctrl.alive.set()
    ctrl.memory_inventory = ctrl.memory_bank_transfer = None
    ctrl.memory_select_read = ctrl.memory_flag_edit = ctrl.memory_channel_pending = None
    ctrl.settings_dialog = ctrl.background_vfo = ctrl.bandscope = None
    ctrl.startup_connect_pending = False
    ctrl.filling_banks = ctrl.monitor_muted = ctrl.operation_signal_open = False
    ctrl.vfo_channel = ctrl.database_station = ctrl.receiving_channel = None
    ctrl.memory_rows = {}
    ctrl.command_console = SimpleNamespace(append=Mock())
    ctrl.now_text = Mock()
    ctrl.cbx_mode = Mock()
    ctrl.cbx_mode.FindString.return_value = 4  # GUI AM -> MD2
    ctrl.clear_memory_import_error = Mock()
    ctrl.update_receiver_switch_ui = Mock()
    ctrl.show_scan_frequency = Mock()
    for method in ('tune_database_record', 'release_monitor_mute', 'clear_vfo_channel',
                   'show_now_receiving', 'receive_context_status', 'receive_activity_context',
                   'mirror_console_line', 'post_console_receive'):
        setattr(ctrl, method, MethodType(getattr(main.AorCtrl, method), ctrl))
    return ctrl


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.colour = patch.object(main.wx.SystemSettings, 'GetColour', return_value='system colour')
        self.colour.start()

    def tearDown(self):
        self.colour.stop()

    def test_database_tune_only_queries_db_preserving_scanner_threshold(self):
        ctrl = controller()
        ctrl.level_squelch_value = 255
        ctrl.operation_selection = lambda: ('Memory Scan', 'Current Bank')
        ctrl.scan_level_context = ('XB', None)
        ctrl.level_readback_timer = None
        ctrl.level_squelch_timer = Mock()
        ctrl.level_squelch_requested = None
        ctrl.update_level_ui = Mock()
        ctrl.settings_dialog = None
        for method in ('level_context', 'refresh_level_context', 'enter_vfo_level_context',
                       'on_level_readback_timeout', 'accept_operation_parameter'):
            setattr(ctrl, method, MethodType(getattr(main.AorCtrl, method), ctrl))
        ctrl.tune_database_record(RECORD)
        self.assertEqual(ctrl.commands[-3:], [b'MC0\r\n', b'RX\r\n', b'DB\r\n'])
        self.assertFalse(any(command.startswith(b'DB0') for command in ctrl.commands))
        self.assertEqual(ctrl.level_context(), ('DB', None))
        ctrl.accept_operation_parameter('DB 255')
        self.assertEqual(ctrl.level_squelch_value, 255)

    def test_tune_sequence_and_identity_confirmed_by_rx_without_fake_channel(self):
        ctrl = controller()
        ctrl.tune_database_record(RECORD)
        self.assertEqual(ctrl.commands, [b'MC1\r\nVA\r\nAU0\r\nRF0005970000\r\nMD2\r\nST005000\r\n',
                                         b'MC0\r\n', b'RX\r\n'])
        self.assertFalse(ctrl.database_station['confirmed'])
        self.assertIsNone(ctrl.vfo_channel)
        ctrl.receive_context_status(STATUS)
        self.assertTrue(ctrl.database_station['confirmed'])
        ctrl.now_text.SetLabel.assert_called_with('BBC World Service')
        self.assertIsNone(ctrl.receiver_status['channel'])
        self.assertIsNone(ctrl.receiving_channel)
        ctrl.receive_context_status(STATUS)
        ctrl.now_text.SetLabel.assert_called_with('BBC World Service')

    def test_mismatch_frequency_mode_step_and_vfo_context_clear_identity(self):
        for response in (STATUS.replace('5970000', '5980000'), STATUS.replace('MD2', 'MD1'),
                         STATUS.replace('ST005000', 'ST010000'), STATUS.replace('VA ', 'VB ')):
            with self.subTest(response=response):
                ctrl = controller()
                ctrl.tune_database_record(RECORD)
                ctrl.receive_context_status(STATUS)
                ctrl.receive_context_status(response)
                self.assertIsNone(ctrl.database_station)

    def test_manual_change_memory_recall_and_scan_status_clear_identity(self):
        for action in ('manual', 'MR', 'MS', 'SM', 'VS'):
            with self.subTest(action=action):
                ctrl = controller()
                ctrl.tune_database_record(RECORD)
                ctrl.receive_context_status(STATUS)
                if action == 'manual':
                    ctrl.clear_vfo_channel()
                elif action == 'VS':
                    ctrl.receive_context_status('VS VA RF0005970000 ST005000 AU0 MD2 AT0')
                else:
                    ctrl.receive_context_status(action + ' MXA02 MP0 RF0005970000 ST005000 AU0 MD2 AT0 TMStation')
                self.assertIsNone(ctrl.database_station)

    def test_matching_lc_keeps_station_and_memory_activity_clears_it(self):
        ctrl = controller()
        ctrl.tune_database_record(RECORD)
        ctrl.receive_context_status(STATUS)
        ctrl.receive_activity_context({'squelch_open': True, 'source': 'VFO', 'source_id': 'A',
                                       'frequency_hz': Decimal(5970000)})
        ctrl.now_text.SetLabel.assert_called_with('BBC World Service')
        ctrl.receive_activity_context({'squelch_open': True, 'source': 'Memory', 'source_id': 'A02',
                                       'frequency_hz': Decimal(5970000)})
        self.assertIsNone(ctrl.database_station)

    def test_tuning_write_failure_still_releases_and_requests_rx(self):
        for raises in (False, True):
            ctrl = controller()
            def write(data):
                ctrl.commands.append(data)
                if data.startswith(b'MC1'):
                    if raises:
                        raise main.serial.SerialException('injected tuning failure')
                    return 0
                return len(data)
            ctrl.write_serial = write
            with redirect_stderr(io.StringIO()):
                ctrl.tune_database_record(RECORD)
            self.assertEqual(ctrl.commands[-2:], [b'MC0\r\n', b'RX\r\n'])
            self.assertFalse(ctrl.monitor_muted)
            self.assertIsNone(ctrl.database_station)
            self.assertEqual(ctrl.commands.count(b'MC0\r\n'), 1)

    def test_failed_mc0_clears_flag_reports_error_and_still_requests_rx(self):
        for raises in (False, True):
            ctrl = controller()
            def write(data):
                ctrl.commands.append(data)
                if data == b'MC0\r\n':
                    if raises:
                        raise main.serial.SerialException('injected release failure')
                    return 0
                return len(data)
            ctrl.write_serial = write
            with redirect_stderr(io.StringIO()) as diagnostic:
                ctrl.tune_database_record(RECORD)
            self.assertFalse(ctrl.monitor_muted)
            self.assertEqual(ctrl.commands[-1], b'RX\r\n')
            self.assertEqual(ctrl.commands.count(b'MC0\r\n'), 1)
            self.assertIn('MC0', diagnostic.getvalue())
            self.assertIn('MC0 failed', ctrl.aor_status.SetStatusText.call_args.args[0])


class ConsoleTests(unittest.TestCase):
    def test_encoding_is_ascii_with_exactly_one_crlf(self):
        ctrl = controller()
        for command in ('MC0', 'RX', 'DB', 'GM'):
            self.assertEqual(command_bytes(command), command.encode('ascii') + b'\r\n')
            send_command(ctrl, command)
        self.assertEqual(ctrl.commands, [b'MC0\r\n', b'RX\r\n', b'DB\r\n', b'GM\r\n'])

    def test_reject_multiple_commands_newlines_and_non_ascii_before_write(self):
        ctrl = controller()
        for command in ('RX\r\n', 'RX\nMC0', 'RX\rMC0', 'ñ', '', '   '):
            with self.subTest(command=command), self.assertRaises(ValueError):
                send_command(ctrl, command)
        self.assertEqual(ctrl.commands, [])

    def test_offline_send_and_controls_disabled(self):
        ctrl = controller()
        ctrl.connected = False
        with self.assertRaises(ValueError):
            send_command(ctrl, 'MC0')
        window = SimpleNamespace(controller=ctrl, command=Mock(), send=Mock(), release=Mock(), read_squelch=Mock())
        CommandConsole.update_connection(window)
        for control in (window.command, window.send, window.release, window.read_squelch):
            control.Enable.assert_called_with(False)
        self.assertEqual(ctrl.commands, [])

    def test_import_export_and_temporary_group_guards_are_not_bypassed(self):
        for operation in ('import', 'export', 'temporary scan'):
            ctrl = controller()
            ctrl.serial.write = Mock()
            ctrl.write_serial = MethodType(main.AorCtrl.write_serial, ctrl)
            if operation == 'import':
                ctrl.memory_bank_transfer = SimpleNamespace(sending=False)
            elif operation == 'export':
                ctrl.memory_inventory = {}
            else:
                ctrl.temporary_scan = SimpleNamespace(sending=False, busy=True, phase='restoring')
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                send_command(ctrl, 'MC0')
            ctrl.serial.write.assert_not_called()
            ctrl.command_console.append.assert_not_called()

    def test_write_mirrors_sent_lines_and_manual_tuning_clears_identity(self):
        ctrl = controller()
        ctrl.write_serial = MethodType(main.AorCtrl.write_serial, ctrl)
        ctrl.serial.write = lambda data: len(data)
        ctrl.database_station = {'station': 'BBC', 'confirmed': True, 'expected': {}}
        with patch.object(main.wx.SystemSettings, 'GetColour', return_value='system colour'):
            send_command(ctrl, 'RX')
            self.assertIsNotNone(ctrl.database_station)  # read-only query preserves provenance
            send_command(ctrl, 'RF0006000000')
        self.assertIsNone(ctrl.database_station)
        self.assertEqual(ctrl.command_console.append.call_args_list[0].args, ('>', 'RX'))
        self.assertEqual(ctrl.command_console.append.call_args_list[1].args, ('>', 'RF0006000000'))

    def test_rx_mirror_does_not_prevent_existing_parser_or_station_confirmation(self):
        ctrl = controller()
        ctrl.set_vfo_text = Mock()
        ctrl.receive_memory_delete_status = ctrl.on_memory_flag_status = Mock()
        ctrl.set_signal_level = Mock()
        ctrl.maybe_load_initial_bank = Mock()
        event = SimpleNamespace(data=[STATUS, 'MC0', 'LM 037'])
        with patch.object(main.wx.SystemSettings, 'GetColour', return_value='system colour'):
            ctrl.tune_database_record(RECORD)
            main.AorCtrl.on_serial_read(ctrl, event)
        ctrl.set_vfo_text.assert_called_once_with(STATUS, 0, active=True)
        ctrl.set_signal_level.assert_called_once_with('LM 037')
        self.assertTrue(ctrl.database_station['confirmed'])
        ctrl.now_text.SetLabel.assert_called_with('BBC World Service')
        self.assertEqual([call.args for call in ctrl.command_console.append.call_args_list],
                         [('<', STATUS), ('<', 'MC0'), ('<', 'LM 037')])

    def test_raw_framing_mirrors_blank_question_and_invalid_lines_without_reinterpreting(self):
        ctrl = controller()
        wire = iter([b'MC0\r\n', b'\r\n', b'?\r\n', b'LMFF \r\n', b'bad\xff\r\n', b'partial'])
        events = []
        def read(delimiter):
            try:
                return next(wire)
            except StopIteration:
                ctrl.alive.clear()
                return b''
        ctrl.serial.read_until = read
        ctrl.GetId = lambda: 7
        ctrl.GetEventHandler = lambda: SimpleNamespace(AddPendingEvent=events.append)
        with redirect_stderr(io.StringIO()):
            main.AorCtrl.com_thread(ctrl)
        self.assertEqual([event.raw_lines for event in events],
                         [['MC0'], [''], ['?'], ['LMFF '], ['bad\\xff'], ['partial']])
        self.assertEqual([event.data for event in events], [['MC0'], [], [], ['LMFF '], [], []])
        cloned = events[3].Clone()
        self.assertEqual(cloned.data, ['LMFF '])
        self.assertEqual(cloned.raw_lines, ['LMFF '])
        self.assertEqual(cloned.GetId(), 7)
        self.assertEqual(cloned.GetEventType(), events[3].GetEventType())

    def test_history_is_bounded_and_preserves_blank_acknowledgements(self):
        history = ConsoleHistory()
        for n in range(600):
            history.append('<', str(n))
        self.assertEqual(len(history.lines), 500)
        self.assertEqual(history.lines[0], '< 100')
        history.append('<', '')
        self.assertEqual(history.lines[-1], '< ')

    def console_window(self, ctrl):
        window = SimpleNamespace(controller=ctrl, history=ConsoleHistory(), manual_send=False,
                                 show_monitoring=Mock(), output=Mock())
        window.show_monitoring.GetValue.return_value = False
        window.append = MethodType(CommandConsole.append, window)
        window.send_text = MethodType(CommandConsole.send_text, window)
        return window

    def test_monitoring_filter_only_affects_console_and_can_be_enabled(self):
        ctrl = controller()
        window = self.console_window(ctrl)
        for prefix in ('<', '>'):
            for value in ('LM 037', 'LM%000', 'LC1', 'LC% Vx RF0093500000'):
                window.append(prefix, value)
        self.assertEqual(list(window.history.lines), [])
        window.append('<', 'DA+023')
        window.append('<', 'DB 057')
        self.assertEqual(list(window.history.lines), ['< DA+023', '< DB 057'])
        window.show_monitoring.GetValue.return_value = True
        window.append('<', 'LM 037')
        self.assertEqual(window.history.lines[-1], '< LM 037')

    def test_manual_monitoring_commands_are_logged_with_reply_filter_notice(self):
        ctrl = controller()
        window = self.console_window(ctrl)
        ctrl.command_console = window
        ctrl.write_serial = MethodType(main.AorCtrl.write_serial, ctrl)
        ctrl.serial.write = lambda data: len(data)
        self.assertTrue(window.send_text('LM'))
        self.assertEqual(window.history.lines[0], '> LM')
        self.assertTrue(window.history.lines[1].startswith('! Enable Show monitoring'))
        self.assertFalse(window.manual_send)

    def test_read_squelch_is_only_two_ordered_queries(self):
        ctrl = controller()
        window = self.console_window(ctrl)
        CommandConsole.on_read_squelch(window, None)
        self.assertEqual(ctrl.commands, [b'DA\r\n', b'DB\r\n'])
        ctrl.commands.clear()
        ctrl.write_serial = lambda data: 0
        CommandConsole.on_read_squelch(window, None)
        self.assertEqual(ctrl.commands, [])
        self.assertTrue(window.history.lines[-1].startswith('! Command was not fully sent'))

    def test_squelch_readback_keeps_actual_marker_and_no_writes(self):
        ctrl = controller()
        ctrl.level_context = lambda: ('DB', None)
        ctrl.level_readback_timer = None
        ctrl.update_level_ui = Mock()
        main.AorCtrl.accept_operation_parameter(ctrl, 'DA+023')
        main.AorCtrl.accept_operation_parameter(ctrl, 'DB 057')
        ctrl.mirror_console_line('<', 'DA+023')
        ctrl.mirror_console_line('<', 'DB 057')
        self.assertEqual([call.args for call in ctrl.command_console.append.call_args_list],
                         [('<', 'DA+023'), ('<', 'DB 057')])
        self.assertEqual(ctrl.level_squelch_value, 57)
        self.assertEqual(ctrl.commands, [])
        with self.assertRaises(ValueError):
            main.AorCtrl.accept_operation_parameter(ctrl, 'DA999')
        self.assertEqual(ctrl.level_squelch_value, 57)


if __name__ == '__main__':
    unittest.main()
