"""Focused scan controls tests; no scanner opens or hardware writes."""
from decimal import Decimal
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_current_bank_scan as scan_tests
from test_current_bank_scan import Controller, main, header, membership
from aor_functions import operation_parameter_command
import wx
from aor_control_frame import AorCtrlFrame


def controller(ctrl=None):
    ctrl = ctrl or Controller()
    ctrl.connected = True
    ctrl.operation_signal_open = True
    ctrl.active_operation = ('Memory Scan', 'Current Bank')
    ctrl.receiver_status = dict(channel='A02', frequency_hz=Decimal(100300000),
                                step_hz=Decimal(100000), mode=0, attenuator=1)
    ctrl.pending_vfo_target = None
    ctrl.pending_vfo_applying = False
    ctrl.pending_vfo_timer = None
    ctrl.vfo = '0093500000'
    ctrl.vfo_status = {2: 'VF RF0093500000 ST100000 AU0 MD0 AT0'}
    ctrl.lb_vfo = Mock()
    ctrl.lb_vfa = Mock()
    ctrl.lb_vfb = Mock()
    ctrl.rb_vfos = Mock()
    ctrl.rb_vfos.GetSelection.return_value = 2
    ctrl.clear_vfo_channel = Mock()
    ctrl.monitor_muted = False
    ctrl.search_restore_command = None
    for name in ('operation_paused', 'paused_operation_target', 'receiver_operation_target', 'on_scan_resume', 'on_scan_to_vfo',
                 'discard_pending_vfo_target', 'pending_vfo_timeout', 'finish_operation_stop',
                 'on_operation_stop', 'release_monitor_mute', 'show_scan_frequency', 'set_vfo_text',
                 'report_scan_error'):
        setattr(ctrl, name, MethodType(getattr(main.AorCtrl, name), ctrl))
    ctrl.cbx_step = Mock()
    ctrl.cbx_step.GetItems.return_value = ['100']
    ctrl.ckbx_auto = Mock()
    ctrl.cbx_mode = Mock()
    ctrl.ckbx_att = Mock()
    ctrl.update_receiver_switch_ui = Mock()
    return ctrl


class ScanControlTests(unittest.TestCase):
    def test_search_pause_requires_signal_and_complete_target_without_memory_id(self):
        for source in ('Range', 'Search Bank', 'Search Group'):
            ctrl = self.scan_receiver()
            ctrl.active_operation = ('Frequency Search', source)
            response = 'VS VA RF0100300000 ST012500 AU0 MD1 AT1'
            ctrl.on_serial_read(SimpleNamespace(data=[response]))
            self.assertFalse(ctrl.operation_paused())
            self.assertIsNone(ctrl.paused_operation_target())
            ctrl.on_scan_resume(None)
            self.assertEqual(ctrl.commands, [])
            ctrl.receive_activity_context(dict(source='VFO', source_id='A',
                                               frequency_hz=100300000, squelch_open=True))
            self.assertTrue(ctrl.operation_signal_open)
            self.assertIsNone(ctrl.paused_operation_target())  # LC alone lacks mode/step/ATT.
            ctrl.on_serial_read(SimpleNamespace(data=[response]))
            self.assertIsNone(ctrl.receiver_status['channel'])
            self.assertTrue(ctrl.operation_paused())
            self.assertEqual(ctrl.paused_operation_target(),
                             dict(frequency_hz=100300000, step_hz=12500, mode=1, attenuator=1))
            ctrl.on_scan_resume(None)
            self.assertEqual(ctrl.commands[-1], b'\x1e\r\n')
            self.assertFalse(ctrl.operation_signal_open)
            self.assertEqual(ctrl.active_operation, ('Frequency Search', source))
            self.assertFalse(any(b'MP' in command or b'GA' in command for command in ctrl.commands))
            ctrl.update_operation_ui.assert_called()

    def test_search_buttons_enable_only_when_genuinely_paused(self):
        ctrl = controller(scan_tests.GuiBoundaryTests().current_bank_start_controller())
        ctrl.bt_resume, ctrl.bt_to_vfo = Mock(), Mock()
        ctrl.active_operation = ('Frequency Search', 'Range')
        ctrl.receiver_status['channel'] = None
        main.AorCtrl.update_operation_ui(ctrl)
        ctrl.bt_resume.Enable.assert_called_with(True)
        ctrl.bt_to_vfo.Enable.assert_called_with(True)
        ctrl.operation_signal_open = False
        main.AorCtrl.update_operation_ui(ctrl)
        ctrl.bt_resume.Enable.assert_called_with(False)
        ctrl.bt_to_vfo.Enable.assert_called_with(False)
        ctrl.operation_signal_open = True
        ctrl.receiver_status['attenuator'] = None
        main.AorCtrl.update_operation_ui(ctrl)
        ctrl.bt_resume.Enable.assert_called_with(False)
        ctrl.bt_to_vfo.Enable.assert_called_with(False)

    def test_search_to_vfo_target_replaces_and_survives_later_search_cursors(self):
        ctrl = self.scan_receiver()
        ctrl.active_operation = ('Frequency Search', 'Range')
        ctrl.on_serial_read(SimpleNamespace(data=['VS VA RF0100300000 ST012500 AU0 MD1 AT1']))
        ctrl.operation_signal_open = True
        ctrl.on_scan_to_vfo(None)
        self.assertEqual(ctrl.commands, [])
        self.assertEqual(ctrl.pending_vfo_target, dict(frequency_hz=100300000, step_hz=12500, mode=1, attenuator=1))
        ctrl.lb_vfo.SetLabel.assert_called_once_with('100.300 MHz')
        ctrl.on_serial_read(SimpleNamespace(data=['VS VB RF0106600000 ST005000 AU0 MD2 AT0']))
        ctrl.lb_vfo.SetLabel.assert_called_once_with('100.300 MHz')
        self.assertEqual(ctrl.pending_vfo_target['frequency_hz'], 100300000)
        ctrl.operation_signal_open = True
        ctrl.on_scan_to_vfo(None)
        self.assertEqual(ctrl.pending_vfo_target, dict(frequency_hz=106600000, step_hz=5000, mode=2, attenuator=0))
        ctrl.lb_vfo.SetLabel.assert_called_with('106.600 MHz')
        self.assertEqual(ctrl.commands, [])
        self.assertEqual(ctrl.vfo, '0093500000')

    def test_search_stop_restores_context_before_applying_pending_one_vfo(self):
        ctrl = self.scan_receiver()
        ctrl.active_operation = ('Frequency Search', 'Range')
        ctrl.normal_vfo_command = lambda: b'VF\r\n'
        ctrl.on_vfo_stop = MethodType(main.AorCtrl.on_vfo_stop, ctrl)
        restore = b'VA\r\nRF0093000000\r\nVB\r\nRF0094000000\r\nVF\r\n'
        ctrl.search_restore_command = restore
        ctrl.on_serial_read(SimpleNamespace(data=['VS VA RF0100300000 ST012500 AU0 MD1 AT1']))
        ctrl.operation_signal_open = True
        ctrl.on_scan_to_vfo(None)
        ctrl.on_operation_stop(None)
        self.assertEqual(ctrl.commands, [b'VF\r\n' + restore + b'RX\r\n',
                                         b'MC1\r\n', b'VF\r\n', b'AU0\r\n', b'RF0100300000\r\n',
                                         b'MD1\r\n', b'ST012500\r\n', b'AT1\r\n', b'MC0\r\n', b'RX\r\n'])
        self.assertIsNone(ctrl.search_restore_command)
        self.assertIsNone(ctrl.active_operation)
        self.assertIsNotNone(ctrl.pending_vfo_target)
        ctrl.on_serial_read(SimpleNamespace(data=['VF RF0100300000 ST012500 AU0 MD1 AT1']))
        self.assertIsNone(ctrl.pending_vfo_target)
        self.assertEqual(ctrl.vfo, '0100300000')
        ctrl.lb_vfo.SetLabel.assert_called_with('100.300 MHz')

    def test_failed_search_restoration_discards_target_without_applying_it(self):
        ctrl = self.scan_receiver()
        ctrl.active_operation = ('Frequency Search', 'Range')
        ctrl.on_vfo_stop = MethodType(main.AorCtrl.on_vfo_stop, ctrl)
        ctrl.search_restore_command = b'VF\r\n'
        ctrl.receiver_status.update(channel=None, mode=1, attenuator=1)
        ctrl.operation_signal_open = True
        ctrl.on_scan_to_vfo(None)
        ctrl.write_serial = Mock(return_value=0)
        ctrl.on_operation_stop(None)
        self.assertIsNone(ctrl.pending_vfo_target)
        self.assertEqual(ctrl.write_serial.call_count, 1)
        self.assertFalse(ctrl.pending_vfo_applying)

    def test_now_receiving_search_stays_dash_and_memory_identity_is_preserved(self):
        ctrl = self.scan_receiver()
        ctrl.now_text = Mock()
        ctrl.show_memory_flags = Mock()
        ctrl.receiving_channel = None
        ctrl.connected = True
        ctrl.show_now_receiving = MethodType(main.AorCtrl.show_now_receiving, ctrl)
        with patch.object(main.wx.SystemSettings, 'GetColour', return_value='system colour'):
            for source in ('Range', 'Search Bank', 'Search Group'):
                ctrl.active_operation = ('Frequency Search', source)
                for opened in (False, True, False):
                    ctrl.operation_signal_open = opened
                    ctrl.receiver_status = dict(context='VS', channel=None, frequency_hz=100300000,
                                                mode=1, step_hz=12500, attenuator=1, name='')
                    ctrl.show_now_receiving()
                    ctrl.now_text.SetLabel.assert_called_with('\u2014')
            ctrl.active_operation = ('Memory Scan', 'Current Bank')
            ctrl.operation_signal_open = True
            ctrl.receiver_status.update(channel='A02', name='Station')
            ctrl.show_now_receiving()
            ctrl.now_text.SetLabel.assert_called_with('A02 \u2014 Station')
            self.assertEqual(ctrl.receiving_channel, 'A02')
            ctrl.operation_signal_open = False
            ctrl.show_now_receiving()
            ctrl.now_text.SetLabel.assert_called_with('Scanning...')
    def scan_receiver(self, original=None):
        ctrl = controller(original)
        ctrl.connected = True
        ctrl.alive = SimpleNamespace(is_set=lambda: True)
        ctrl.memory_bank_transfer = ctrl.memory_inventory = None
        ctrl.memory_channel_pending = ctrl.memory_select_read = None
        ctrl.background_vfo = ctrl.bandscope = None
        ctrl.startup_connect_pending = False
        ctrl.vfo_channel = ctrl.database_station = None
        ctrl.memory_rows = {}
        ctrl.clear_memory_import_error = Mock()
        ctrl.mirror_console_line = Mock()
        ctrl.receive_memory_delete_status = ctrl.on_memory_flag_status = Mock()
        ctrl.maybe_load_initial_bank = Mock()
        ctrl.receive_context_status = MethodType(main.AorCtrl.receive_context_status, ctrl)
        ctrl.receive_activity_context = MethodType(main.AorCtrl.receive_activity_context, ctrl)
        ctrl.on_serial_read = MethodType(main.AorCtrl.on_serial_read, ctrl)
        ctrl.operation_signal_open = False
        return ctrl

    def test_scan_rx_keeps_pre_scan_one_vfo_display_and_full_cached_state(self):
        ctrl = self.scan_receiver()
        confirmed = dict(ctrl.vfo_status)
        for channel, frequency in (('A02', '0100300000'), ('A03', '0106600000')):
            ctrl.on_serial_read(SimpleNamespace(data=[
                'MS MX%s MP0 RF%s ST100000 AU0 MD0 AT1 TMStation' % (channel, frequency)]))
            ctrl.lb_vfo.SetLabel.assert_not_called()
            self.assertEqual(ctrl.vfo, '0093500000')
            self.assertEqual(ctrl.vfo_status, confirmed)
            self.assertEqual(ctrl.receiver_status['channel'], channel)
            self.assertEqual(ctrl.receiver_status['frequency_hz'], int(frequency))
            ctrl.show_now_receiving.assert_called()

    def test_search_rx_does_not_update_any_vfo_cache_or_labels(self):
        ctrl = self.scan_receiver()
        ctrl.active_operation = ('Frequency Search', 'Range')
        for prefix in ('VS', 'VV'):
            for source in ('VA', 'VB', 'VF'):
                ctrl.on_serial_read(SimpleNamespace(data=[
                    '%s %s RF0100300000 ST012500 AU0 MD1 AT0' % (prefix, source)]))
                self.assertEqual(ctrl.vfo_status, {2: 'VF RF0093500000 ST100000 AU0 MD0 AT0'})
                self.assertEqual(ctrl.vfo, '0093500000')
                self.assertEqual(ctrl.receiver_status['frequency_hz'], 100300000)
                for label in (ctrl.lb_vfa, ctrl.lb_vfb, ctrl.lb_vfo):
                    label.SetLabel.assert_not_called()
        ctrl.aor_status.SetStatusText.assert_called_with('Frequency search: 100.300000 MHz')

    def test_scan_rx_and_activity_keep_deferred_target_until_next_to_vfo(self):
        ctrl = self.scan_receiver()
        ctrl.on_serial_read(SimpleNamespace(data=[
            'MS MXA02 MP0 RF0100300000 ST100000 AU0 MD0 AT1 TMStation']))
        ctrl.operation_signal_open = True
        ctrl.on_scan_to_vfo(None)
        self.assertEqual(ctrl.pending_vfo_target['frequency_hz'], 100300000)
        ctrl.lb_vfo.SetLabel.assert_called_once_with('100.300 MHz')
        ctrl.on_serial_read(SimpleNamespace(data=[
            'MS MXA03 MP0 RF0106600000 ST100000 AU0 MD0 AT1 TMStation']))
        ctrl.receive_activity_context(dict(source='Memory', source_id='A03',
                                           frequency_hz=106600000, squelch_open=True))
        ctrl.lb_vfo.SetLabel.assert_called_once_with('100.300 MHz')
        ctrl.receiver_status.update(mode=0, step_hz=100000, attenuator=1)
        ctrl.on_scan_to_vfo(None)
        ctrl.lb_vfo.SetLabel.assert_called_with('106.600 MHz')
        self.assertEqual(ctrl.pending_vfo_target['frequency_hz'], 106600000)
        self.assertEqual(ctrl.vfo, '0093500000')
        self.assertEqual(ctrl.active_operation, ('Memory Scan', 'Current Bank'))

    def test_stop_without_to_vfo_preserves_pre_scan_vf_and_confirmed_readback(self):
        ctrl = self.scan_receiver()
        ctrl.normal_vfo_command = lambda: b'VF\r\n'
        ctrl.on_serial_read(SimpleNamespace(data=[
            'MS MXA02 MP0 RF0100300000 ST100000 AU0 MD0 AT1 TMStation']))
        ctrl.on_operation_stop(None)
        self.assertEqual(ctrl.commands, [b'VF\r\nRX\r\n'])
        self.assertEqual(ctrl.vfo, '0093500000')
        ctrl.lb_vfo.SetLabel.assert_not_called()
        ctrl.on_serial_read(SimpleNamespace(data=['VF RF0093500000 ST100000 AU0 MD0 AT0']))
        ctrl.lb_vfo.SetLabel.assert_called_with('93.500 MHz')
        self.assertEqual(ctrl.vfo_status[2], 'VF RF0093500000 ST100000 AU0 MD0 AT0')

    def test_temporary_group_stop_without_target_preserves_one_vfo_through_restoration(self):
        fixture = scan_tests.ScanTests()
        original, scan = fixture.prepare()
        ctrl = self.scan_receiver(original)
        ctrl.restore_temporary_scan = MethodType(main.AorCtrl.restore_temporary_scan, ctrl)
        ctrl.scan_lifecycle_action = None
        ctrl.on_serial_read(SimpleNamespace(data=[
            'MS MXJ02 MP0 RF0446500000 ST012500 AU0 MD1 AT0 TMPMR']))
        ctrl.on_operation_stop(None)
        self.assertEqual(scan.phase, 'restoring')
        fixture.feed_group(scan, 2, '')
        fixture.feed_group(scan, 4, 'BCD')
        self.assertIsNone(ctrl.temporary_scan)
        self.assertEqual(ctrl.vfo, '0093500000')
        self.assertEqual(ctrl.vfo_status[2], 'VF RF0093500000 ST100000 AU0 MD0 AT0')
        ctrl.lb_vfo.SetLabel.assert_not_called()
        self.assertFalse(any(b'RF' in command or command == b'VF\r\n' for command in ctrl.commands))

    def test_xp_99_and_other_dwell_limits_unchanged(self):
        self.assertEqual(operation_parameter_command('XP', 99), b'XP99\r\n')
        self.assertEqual(operation_parameter_command('XP', 0), b'XP00\r\n')
        for key, value in (('XP', 100), ('DP', 99), ('SP', 99)):
            with self.assertRaises(ValueError):
                operation_parameter_command(key, value)

    def test_resume_sends_only_arrow_and_scan_continues(self):
        ctrl = controller()
        ctrl.on_scan_resume(None)
        self.assertEqual(ctrl.commands, [b'\x1e\r\n'])
        self.assertFalse(ctrl.operation_signal_open)
        self.assertEqual(ctrl.active_operation, ('Memory Scan', 'Current Bank'))
        ctrl.on_scan_resume(None)
        self.assertEqual(len(ctrl.commands), 1)
        ctrl.show_now_receiving.assert_called_once()

    def test_to_vfo_is_display_only_and_second_press_replaces_target(self):
        ctrl = controller()
        confirmed = dict(ctrl.vfo_status)
        ctrl.on_scan_to_vfo(None)
        ctrl.lb_vfo.SetLabel.assert_called_with('100.300 MHz')
        self.assertEqual(ctrl.vfo, '0093500000')
        self.assertEqual(ctrl.vfo_status, confirmed)
        ctrl.receiver_status['frequency_hz'] = Decimal(106600000)
        ctrl.on_scan_to_vfo(None)
        self.assertEqual(ctrl.pending_vfo_target['frequency_hz'], 106600000)
        self.assertEqual(ctrl.commands, [])
        self.assertTrue(ctrl.operation_signal_open)
        self.assertIsNotNone(ctrl.active_operation)
        ctrl.show_scan_frequency(107000000)
        ctrl.lb_vfo.SetLabel.assert_called_with('106.600 MHz')

    def test_incomplete_or_moving_channel_cannot_be_saved(self):
        ctrl = controller()
        ctrl.receiver_status['attenuator'] = None
        self.assertIsNone(ctrl.paused_operation_target())
        ctrl.on_scan_to_vfo(None)
        self.assertIsNone(ctrl.pending_vfo_target)
        ctrl.operation_signal_open = False
        self.assertFalse(ctrl.operation_paused())

    def test_buttons_only_enabled_for_paused_memory_scan(self):
        ctrl = controller(scan_tests.GuiBoundaryTests().current_bank_start_controller())
        ctrl.bt_resume, ctrl.bt_to_vfo = Mock(), Mock()
        main.AorCtrl.update_operation_ui(ctrl)
        ctrl.bt_resume.Enable.assert_called_with(True)
        ctrl.bt_to_vfo.Enable.assert_called_with(True)
        ctrl.receiver_status['attenuator'] = None
        main.AorCtrl.update_operation_ui(ctrl)
        ctrl.bt_to_vfo.Enable.assert_called_with(False)
        ctrl.operation_signal_open = False
        main.AorCtrl.update_operation_ui(ctrl)
        ctrl.bt_resume.Enable.assert_called_with(False)
        ctrl.bt_to_vfo.Enable.assert_called_with(False)

    def test_stop_without_saved_target_preserves_existing_path(self):
        ctrl = controller()
        ctrl.on_operation_stop(None)
        self.assertEqual(ctrl.commands, [b'VA\r\nRX\r\n'])
        self.assertIsNone(ctrl.active_operation)
        ctrl.enter_vfo_level_context.assert_called_once()

    def test_stop_waits_for_real_group_restoration_before_tuning(self):
        fixture = scan_tests.ScanTests()
        original, scan = fixture.prepare(dwell=99)
        ctrl = controller(original)
        ctrl.restore_temporary_scan = MethodType(main.AorCtrl.restore_temporary_scan, ctrl)
        ctrl.scan_lifecycle_action = None
        ctrl.on_scan_to_vfo(None)
        ctrl.commands.clear()
        ctrl.on_operation_stop(None)
        self.assertEqual(scan.phase, 'restoring')
        self.assertFalse(any(b'VF\r\n' in c or b'RF' in c for c in ctrl.commands))
        scan_tests.drain_pacing(scan)
        scan.receive(header(2, dwell=99))
        scan.receive(membership(''))
        self.assertFalse(any(b'VF\r\n' in c for c in ctrl.commands))
        scan_tests.drain_pacing(scan)
        scan.receive(header(4, dwell=99))
        scan.receive(membership('BCD'))
        self.assertIsNone(ctrl.temporary_scan)
        self.assertEqual(ctrl.commands[-10:], [b'RX\r\n', b'MC1\r\n', b'VF\r\n', b'AU0\r\n',
                                             b'RF0100300000\r\n', b'MD0\r\n', b'ST100000\r\n',
                                             b'AT1\r\n', b'MC0\r\n', b'RX\r\n'])
        self.assertTrue(ctrl.pending_vfo_applying)
        self.assertIsNotNone(ctrl.pending_vfo_target)
        ctrl.set_vfo_text('VF RF0093500000 ST100000 AU0 MD0 AT0', 2, active=False)
        ctrl.lb_vfo.SetLabel.assert_called_with('100.300 MHz')
        ctrl.set_vfo_text('VF RF0100300000 ST100000 AU0 MD0 AT1', 2, active=False)
        self.assertIsNone(ctrl.pending_vfo_target)
        self.assertEqual(ctrl.vfo, '0100300000')
        ctrl.rb_vfos.SetSelection.assert_called_with(2)

    def test_error_discards_saved_target_without_tuning(self):
        ctrl = controller()
        ctrl.on_scan_to_vfo(None)
        ctrl.report_scan_error('Simulated scan failure')
        self.assertIsNone(ctrl.pending_vfo_target)
        self.assertEqual(ctrl.commands, [])
        ctrl.lb_vfo.SetLabel.assert_called_with('93.500 MHz')

    def test_unverified_application_times_out_without_retry(self):
        ctrl = controller()
        ctrl.on_scan_to_vfo(None)
        ctrl.active_operation = None
        ctrl.finish_operation_stop()
        sent = list(ctrl.commands)
        ctrl.pending_vfo_timeout()
        self.assertEqual(ctrl.commands, sent)
        self.assertIsNone(ctrl.pending_vfo_target)
        self.assertFalse(ctrl.pending_vfo_applying)

    def test_disconnect_and_close_discard_before_restore_callback(self):
        for name in ('stop_thread', 'close_serial', 'on_close'):
            ctrl = controller()
            ctrl.on_scan_to_vfo(None)
            ctrl.temporary_scan = Mock()
            ctrl.restore_temporary_scan = Mock()
            ctrl.stop_thread = Mock()
            ctrl.close_serial = Mock()
            ctrl.disconnect = Mock()
            ctrl.on_close = Mock()
            event = Mock()
            if name == 'on_close':
                main.AorCtrl.on_close(ctrl, event)
            else:
                getattr(main.AorCtrl, name)(ctrl)
            self.assertIsNone(ctrl.pending_vfo_target)
            self.assertEqual(ctrl.commands, [])

    def test_failed_apply_releases_audio_and_discards(self):
        ctrl = controller()
        ctrl.on_scan_to_vfo(None)
        ctrl.active_operation = None
        def write(command):
            ctrl.commands.append(command)
            return 0 if command.startswith(b'RF') else len(command)
        ctrl.write_serial = write
        ctrl.finish_operation_stop()
        self.assertIn(b'MC0\r\n', ctrl.commands)
        self.assertEqual(ctrl.commands[-1], b'RX\r\n')
        self.assertIsNone(ctrl.pending_vfo_target)
        self.assertFalse(ctrl.monitor_muted)


class ScanControlLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.GetApp() or wx.App(False)

    def test_four_compact_buttons_and_memory_scan_dwell_choices(self):
        frame = AorCtrlFrame(None)
        dialog = None
        try:
            frame.Show()
            frame.Layout()
            wx.Yield()
            buttons = [frame.bt_start, frame.bt_stop, frame.bt_resume, frame.bt_to_vfo]
            self.assertEqual([b.GetLabel() for b in buttons], ['Start', 'Stop', 'Resume', 'To VFO'])
            self.assertEqual(len({b.GetPosition().y for b in buttons}), 1)
            self.assertLessEqual(buttons[-1].GetRect().right, frame.operation_panel.GetClientSize().width)
            self.assertFalse(frame.bt_resume.IsEnabled())
            self.assertFalse(frame.bt_to_vfo.IsEnabled())
            frame.active_operation = None
            dialog = main.OperationSettingsDialog(frame, 'X', 1)
            self.assertEqual(dialog.options['XP'], list(range(100)))
            self.assertEqual(dialog.controls['XP'].GetString(99), '99 s')
        finally:
            if dialog is not None:
                dialog.Destroy()
            frame.Destroy()
            wx.Yield()


class GuiActionTests(unittest.TestCase):
    def test_idle_stored_range_settings_are_local_and_keep_db_unchanged(self):
        frame = self.connected_frame()
        try:
            self.seed_search_banks(frame)
            frame.operation_choice.SetSelection(1)
            frame.source_choice.SetItems(['Manual Range', 'Stored Range', 'Linked Ranges'])
            frame.source_choice.SetSelection(1)
            frame.level_squelch_value = 255
            frame.serial.write.reset_mock()
            frame.open_operation_settings(None)
            dialog = frame.settings_dialog
            self.assertIsInstance(dialog, main.StoredRangeSettingsDialog)
            self.assertIsNone(frame.temporary_scan)
            self.assertEqual(frame.level_context(), ('DB', None))
            self.assertEqual(dialog.values, dict(SA=0, SB=None, SD=20, SP=0))
            dialog.controls['SB'].SetSelection(dialog.options['SB'].index(42))
            dialog.controls['SP'].SetSelection(dialog.options['SP'].index(10))
            dialog.on_changed(None)
            dialog.on_save(None)
            self.assertEqual(frame.stored_range_profiles['A'], dict(SA=0, SB=42, SD=20, SP=10))
            frame.serial.write.assert_not_called()
            self.assertIsNone(frame.active_operation)
            self.assertEqual(frame.level_squelch_value, 255)
            dialog.controls['SB'].SetSelection(0)
            dialog.on_changed(None)
            dialog.on_save(None)
            self.assertIsNone(frame.stored_range_profiles['A']['SB'])
        finally:
            if frame.settings_dialog is not None:
                frame.settings_dialog.Destroy()
                frame.settings_dialog = None
            self.close_search_test(frame)

    def test_active_stored_range_settings_use_live_sb_and_save_session_override(self):
        from stored_range_search import StoredRangeSearch
        import test_stored_range_search as search_tests
        frame = self.connected_frame()
        try:
            self.seed_search_banks(frame)
            frame.operation_choice.SetSelection(1)
            frame.source_choice.SetItems(['Manual Range', 'Stored Range', 'Linked Ranges'])
            frame.source_choice.SetSelection(1)
            frame.level_squelch_value = 255
            # Drive the production state machine with an offline clock and mocked serial writes.
            fake = search_tests.controller()
            frame.scan_call_later = fake.scan_call_later
            frame.on_start_search_bank(None, bank='A')
            scan = frame.temporary_scan
            self.assertIsInstance(scan, StoredRangeSearch)
            search_tests.feed(scan, 0, '', level=0, voice=0, delay='2.0', dwell=0)
            search_tests.feed(scan, 1, '')
            search_tests.activate(frame, scan)
            self.assertEqual(scan.phase, 'active')
            self.assertEqual(frame.level_context(), ('SB', 1))
            frame.serial.write.reset_mock()
            frame.open_operation_settings(None)
            dialog = frame.settings_dialog
            self.assertNotIsInstance(dialog, main.StoredRangeSettingsDialog)
            self.assertEqual((dialog.family, dialog.group), ('S', 1))
            self.assertEqual(frame.serial.write.call_args.args[0], b'GS\r\n')
            frame.set_group_response('GS1 SD2.0 SB 255 SA 000 SP00 AS0')
            frame.set_group_response(search_tests.bs('A'))
            dialog.controls['SB'].SetSelection(dialog.options['SB'].index(44))
            dialog.controls['SA'].SetSelection(dialog.options['SA'].index(2))
            dialog.on_changed(None)
            frame.serial.write.reset_mock()
            dialog.on_save(None)
            self.assertEqual([call.args[0] for call in frame.serial.write.call_args_list],
                             [b'SB044\r\nSA002\r\n', b'GS\r\n'])
            frame.set_group_response('GS1 SD2.0 SB 044 SA 002 SP00 AS0')
            frame.set_group_response(search_tests.bs('A'))
            self.assertEqual(frame.stored_range_profiles['A'], dict(SA=2, SB=44, SD=20, SP=0))
            self.assertEqual(frame.sql.GetValue(), 44)
            self.assertEqual(frame.level_squelch_value, 255)
            self.assertEqual(scan.saved['values']['SB'], 7)
            self.assertEqual(scan.phase, 'active')
        finally:
            if frame.settings_dialog is not None:
                frame.settings_dialog.Destroy()
                frame.settings_dialog = None
            if frame.temporary_scan is not None:
                frame.temporary_scan.cancel_sequence()
                frame.temporary_scan.timer.Stop()
            frame.temporary_scan = None
            self.close_search_test(frame)
    @classmethod
    def setUpClass(cls):
        cls.app = wx.GetApp() or wx.App(False)

    def setUp(self):
        self.no_port = patch.object(main.serial.Serial, 'open', side_effect=AssertionError('No hardware in GUI tests'))
        self.no_port.start()
        self.frame = main.AorCtrl(None)
        self.frame.Show()
        wx.Yield()

    def tearDown(self):
        self.frame.Destroy()
        wx.Yield()
        self.no_port.stop()

    def test_toolbar_settings_replaces_body_buttons_and_dispatches_existing_handler(self):
        frame = self.frame
        self.assertFalse(hasattr(frame, 'bt_newchannel'))
        self.assertFalse(hasattr(frame, 'bt_settings'))
        toolbar = frame.GetToolBar()
        self.assertEqual([toolbar.GetToolByPos(n).GetLabel() for n in range(toolbar.GetToolsCount())],
                         ['Upload', 'Connect', 'Serial Config', 'Operation Settings'])
        self.assertFalse(toolbar.GetToolEnabled(frame.operation_settings_tool_id))
        self.assertEqual(toolbar.GetToolBitmapSize(), wx.Size(32, 32))
        settings_tool = toolbar.FindById(frame.operation_settings_tool_id)
        self.assertNotEqual(settings_tool.GetNormalBitmap().ConvertToImage().GetData(),
                            toolbar.FindById(frame.config_tool_id).GetNormalBitmap().ConvertToImage().GetData())
        disabled_image = settings_tool.GetDisabledBitmap().ConvertToImage()
        self.assertTrue(disabled_image.IsOk())
        self.assertGreater(len(set(disabled_image.GetData())), 1)
        frame.open_operation_settings = Mock()
        event = wx.CommandEvent(wx.EVT_TOOL.typeId, frame.operation_settings_tool_id)
        frame.GetEventHandler().ProcessEvent(event)
        frame.open_operation_settings.assert_called_once_with(event)
        self.assertEqual(frame.sql.GetMinSize().width, 220)
        self.assertEqual(frame.sql.GetParent(), frame.panel_1)

    def capture_menu(self, row=None):
        captured = []
        def capture(menu):
            captured.extend((item.GetItemLabel(), item.IsEnabled()) for item in menu.GetMenuItems())
            # Invoke New exactly through the menu's binding, not a second path.
            event = wx.CommandEvent(wx.EVT_MENU.typeId, menu.GetMenuItems()[0].GetId())
            menu.ProcessEvent(event)
        frame = self.frame
        frame.open_memory_channel = Mock()
        with patch.object(frame.edit_list.memory, 'PopupMenu', side_effect=capture):
            frame.open_memory_flag_menu(row)
        return captured

    def test_memory_context_menu_order_and_empty_bank_new_action(self):
        frame = self.frame
        frame.selected_memory_bank = lambda: {'bank': 'A', 'channels': 70}
        frame.memory_channel_available = lambda: True
        frame.memory_flags_editable = lambda channel: True
        frame.memory_empty_channels = {'A01'}
        frame.memory_rows['A00'] = {'select': True, 'skip': False}
        frame.edit_list.memory_model.channels[:] = ['A00']
        menu = self.capture_menu(0)
        self.assertEqual([label for label, enabled in menu],
                         ['New Channel...', 'Edit Channel...', 'Delete Channel...', '',
                          'Include in Selected Channels', 'Skip during Stored Channels scanning', '',
                          'Select all channels', 'Deselect all channels', 'Skip all channels', 'Clear all skips'])
        self.assertTrue(menu[0][1])
        frame.open_memory_channel.assert_called_once_with()
        frame.memory_rows.clear()
        frame.edit_list.memory_model.channels.clear()
        self.assertEqual(self.capture_menu(), [('New Channel...', True), ('', True),
                                              ('Select all channels', False), ('Deselect all channels', False),
                                              ('Skip all channels', False), ('Clear all skips', False)])
        frame.open_memory_channel.assert_called_once_with()

    def test_new_disabled_without_empty_slot_or_edit_permission(self):
        frame = self.frame
        frame.selected_memory_bank = lambda: {'bank': 'A', 'channels': 1}
        frame.memory_channel_available = lambda: True
        frame.memory_empty_channels.clear()
        self.assertEqual(self.capture_menu()[0], ('New Channel...', False))
        frame.memory_empty_channels.add('A00')
        frame.memory_channel_available = lambda: False
        self.assertEqual(self.capture_menu()[0], ('New Channel...', False))
        frame.selected_memory_bank = lambda: None
        with patch.object(frame.edit_list.memory, 'PopupMenu') as popup:
            frame.open_memory_flag_menu()
        popup.assert_not_called()

    def test_empty_space_context_event_and_toolbar_enable_rules(self):
        frame = self.frame
        event = Mock()
        event.GetItem.return_value = main.dv.NullDataViewItem
        with patch.object(frame, 'open_memory_flag_menu') as menu:
            frame.on_memory_context_menu(event)
        menu.assert_called_once_with(None)
        with patch.object(frame.edit_list.memory, 'HitTest', return_value=(main.dv.NullDataViewItem, None)), \
                patch.object(frame, 'open_memory_flag_menu') as menu:
            frame.on_memory_right_down(event)
        menu.assert_called_once_with()
        event.Skip.assert_not_called()
        item = Mock()
        item.IsOk.return_value = True
        with patch.object(frame.edit_list.memory, 'HitTest', return_value=(item, None)), \
                patch.object(frame, 'open_memory_flag_menu') as menu:
            frame.on_memory_right_down(event)
        menu.assert_not_called()
        event.Skip.assert_called_once_with()
        frame.connected = True
        frame.serial.is_open = True
        frame.alive.set()
        try:
            frame.update_operation_ui()
            self.assertTrue(frame.GetToolBar().GetToolEnabled(frame.operation_settings_tool_id))
            frame.active_operation = ('Memory Scan', 'Current Bank')
            frame.update_operation_ui()
            self.assertFalse(frame.GetToolBar().GetToolEnabled(frame.operation_settings_tool_id))
            frame.active_operation = None
            frame.memory_bank_transfer = Mock()
            frame.update_operation_ui()
            self.assertFalse(frame.GetToolBar().GetToolEnabled(frame.operation_settings_tool_id))
        finally:
            frame.memory_bank_transfer = None
            frame.alive.clear()
            frame.serial.is_open = False

    def connected_frame(self):
        frame = self.frame
        frame.connected = True
        frame.serial.is_open = True
        frame.alive.set()
        frame.serial.write = Mock(side_effect=len)
        return frame

    def seed_search_banks(self, frame):
        frame.current_list_view = lambda: 'LOG VIEW'
        for line in ('SRA SL0001500000 SU0001700000 ST009000 AU0 MD2 AT0 TTMW',
                     'SRB ---',
                     'SRC SL0118000000 SU0137000000 ST012500 AU0 MD2 AT0 TTC',
                     'SRa ---'):
            frame.set_search_banks(line)

    def close_search_test(self, frame):
        if frame.search_bank_dialog is not None:
            frame.search_bank_dialog.Destroy()
            frame.search_bank_dialog = None
        frame.alive.clear()
        frame.serial.is_open = False

    def test_stored_range_selector_contains_only_known_populated_banks_and_preserves_identity(self):
        frame = self.connected_frame()
        try:
            self.assertEqual(frame.search_bank_choice.GetCount(), 0)
            self.seed_search_banks(frame)
            self.assertEqual(frame.search_bank_choice.GetItems(), ['MW', 'Unnamed range'])
            self.assertEqual([frame.search_bank_choice.GetClientData(n) for n in range(2)], ['A', 'C'])
            frame.search_bank_choice.SetSelection(1)
            frame.set_search_banks('SRD SL0001500000 SU0001700000 ST009000 AU0 MD2 AT0 TTMW')
            frame.set_search_banks('SRC SL0118000000 SU0137000000 ST012500 AU0 MD2 AT0 TTAIR')
            self.assertEqual(frame.search_bank_choice.GetItems(), ['MW', 'AIR', 'MW'])
            index = frame.search_bank_choice.GetSelection()
            self.assertEqual(frame.search_bank_choice.GetClientData(index), 'C')
            frame.update_search_bank_choices()
            self.assertEqual(frame.search_bank_choice.GetClientData(frame.search_bank_choice.GetSelection()), 'C')
        finally:
            self.close_search_test(frame)

    def test_duplicate_named_ranges_start_correct_internal_bank_and_preserve_selection(self):
        import test_stored_range_search as search_tests
        frame = self.connected_frame()
        try:
            self.seed_search_banks(frame)
            frame.set_search_banks('SRD SL0001500000 SU0001700000 ST009000 AU0 MD2 AT0 TTMW')
            frame.operation_choice.SetSelection(1)
            frame.source_choice.SetItems(['Manual Range', 'Stored Range', 'Linked Ranges'])
            frame.source_choice.SetSelection(1)
            frame.level_squelch_value = 255
            frame.scan_call_later = search_tests.controller().scan_call_later
            for bank in ('A', 'D'):
                index = next(n for n in range(frame.search_bank_choice.GetCount())
                             if frame.search_bank_choice.GetClientData(n) == bank)
                frame.search_bank_choice.SetSelection(index)
                self.assertEqual(frame.search_bank_choice.GetStringSelection(), 'MW')
                frame.active_operation = None
                frame.serial.write.reset_mock()
                frame.on_operation_start(None)
                scan = frame.temporary_scan
                self.assertEqual(scan.target, bank)
                search_tests.feed(scan, 0, '', level=0, voice=0, delay='2.0', dwell=0)
                search_tests.feed(scan, 1, '')
                scan_tests.drain_pacing(scan)
                scan.receive('GS1 SD2.0 SB 255 SA 000 SP00 AS0')
                scan.receive(search_tests.bs(bank))
                scan_tests.drain_pacing(scan)
                self.assertEqual([call.args[0] for call in frame.serial.write.call_args_list][-3:],
                                 [b'GS1\r\n', ('SS%s\r\n' % bank).encode('ascii'), b'RX\r\n'])
                scan.cancel_sequence()
                scan.timer.Stop()
                frame.temporary_scan = None
            frame.update_search_bank_choices()
            self.assertEqual(frame.search_bank_choice.GetClientData(frame.search_bank_choice.GetSelection()), 'D')
            self.assertEqual(frame.search_bank_choice.GetStringSelection(), 'MW')
        finally:
            self.close_search_test(frame)

    def test_named_list_rows_edit_correct_fixed_bank_without_parsing_the_visible_name(self):
        frame = self.connected_frame()
        try:
            frame.current_list_view = lambda: 'SEARCH BANKS'
            frame.prepare_list((('Stored Range', 85), ('Lower', 125), ('Upper', 125), ('Step', 100),
                                ('Auto', 45), ('Mode', 55), ('Att', 40), ('Name', 130)))
            for bank in ('A', 'D'):
                frame.set_search_banks('SR%s SL0001500000 SU0001700000 ST009000 AU0 MD2 AT0 TTMW' % bank)
                self.assertEqual(frame.edit_list.list.GetItemText(frame.search_bank_rows[bank]), 'MW')
            row = frame.search_bank_rows['D']
            frame.edit_list.list.Select(row)
            self.assertEqual(frame.selected_search_bank(), 'D')
            def choose_edit(menu):
                item = menu.GetMenuItems()[0]
                menu.ProcessEvent(wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId()))
            with patch.object(frame.edit_list.list, 'PopupMenu', side_effect=choose_edit):
                frame.open_menu(SimpleNamespace(GetIndex=lambda: row))
            dialog = frame.search_bank_dialog
            self.assertEqual(dialog.GetTitle(), 'Edit Stored Range - MW')
            self.assertEqual(dialog.bank.GetItems(), ['MW'])
            self.assertEqual(dialog.selected_bank(), 'D')
            self.assertFalse(dialog.bank.IsEnabled())
            self.assertNotIn('[D]', dialog.GetTitle())
            frame.set_search_banks('SRD SL0001500000 SU0001700000 ST009000 AU0 MD2 AT0 TTMW')
            dialog.name.SetValue('MW UPDATED')
            dialog.on_save(None)
            self.assertTrue(frame.serial.write.call_args.args[0].startswith(b'SED '))
            self.assertTrue(frame.serial.write.call_args.args[0].endswith(b'\r\nSRD\r\n'))
        finally:
            self.close_search_test(frame)

    def test_create_dialog_offers_empty_slots_only_and_no_overwrite_on_changed_state(self):
        frame = self.connected_frame()
        try:
            self.seed_search_banks(frame)
            frame.open_search_bank(create=True)
            dialog = frame.search_bank_dialog
            self.assertTrue(dialog.creating)
            self.assertEqual(dialog.bank.GetItems(), ['B', 'a'])
            self.assertEqual(frame.serial.write.call_args.args[0], b'SRB\r\n')
            frame.set_search_banks('SRB ---')
            dialog.bank.SetStringSelection('a')
            dialog.on_bank(None)
            self.assertEqual(frame.serial.write.call_args.args[0], b'SRa\r\n')
            frame.set_search_banks('SRa ---')
            dialog.lower.SetValue('5')
            dialog.upper.SetValue('6')
            dialog.name.SetValue('Test')
            frame.search_banks['a'] = dict(bank='a', empty=False)  # Occupied after the dialog opened.
            frame.serial.write.reset_mock()
            dialog.on_save(None)
            frame.serial.write.assert_not_called()
            self.assertFalse(dialog.save.IsEnabled())
            self.assertIn('Slot state changed', dialog.message.GetLabel())
        finally:
            self.close_search_test(frame)

    def test_create_refuses_when_no_confirmed_empty_slots_exist(self):
        frame = self.connected_frame()
        try:
            self.seed_search_banks(frame)
            frame.search_banks = {bank: entry for bank, entry in frame.search_banks.items() if not entry['empty']}
            frame.open_search_bank(create=True)
            self.assertIsNone(frame.search_bank_dialog)
            self.assertEqual(frame.aor_status.GetStatusText(), 'No empty Stored Range slots are available.')
            frame.search_banks.clear()  # Unknown slots must not be offered as empty.
            frame.open_search_bank(create=True)
            self.assertIsNone(frame.search_bank_dialog)
        finally:
            self.close_search_test(frame)

    def test_edit_is_fixed_to_populated_bank_and_never_becomes_create(self):
        frame = self.connected_frame()
        try:
            self.seed_search_banks(frame)
            frame.open_search_bank(bank='B')
            self.assertIsNone(frame.search_bank_dialog)
            frame.operation_choice.SetSelection(1)
            frame.source_choice.SetItems(['Manual Range', 'Stored Range', 'Linked Ranges'])
            frame.source_choice.SetSelection(1)
            frame.search_bank_choice.SetSelection(0)
            frame.open_search_bank()  # Operation selector uses the internal client-data ID.
            dialog = frame.search_bank_dialog
            self.assertFalse(dialog.creating)
            self.assertEqual(dialog.bank.GetItems(), ['MW'])
            self.assertFalse(dialog.bank.IsEnabled())
            self.assertEqual(dialog.GetTitle(), 'Edit Stored Range - MW')
            self.assertEqual(dialog.selected_bank(), 'A')
            self.assertNotIn('[A]', dialog.GetTitle())
            frame.set_search_banks('SRA SL0001500000 SU0001700000 ST009000 AU0 MD2 AT0 TTMW')
            self.assertTrue(dialog.save.IsEnabled())
            frame.set_search_banks('SRA ---')
            dialog.show_bank(frame.search_banks['A'])
            self.assertFalse(dialog.save.IsEnabled())
            self.assertFalse(dialog.creating)
        finally:
            self.close_search_test(frame)

    def test_unknown_stored_ranges_have_no_placeholder_selection_and_start_is_safe(self):
        frame = self.connected_frame()
        try:
            frame.operation_choice.SetSelection(1)
            frame.source_choice.SetItems(['Manual Range', 'Stored Range', 'Linked Ranges'])
            frame.source_choice.SetSelection(1)
            self.assertEqual(frame.search_bank_choice.GetSelection(), wx.NOT_FOUND)
            frame.on_operation_start(None)
            frame.serial.write.assert_not_called()
            self.assertIsNone(frame.active_operation)
            frame.on_search_source_changed(None)
            frame.serial.write.assert_not_called()
        finally:
            self.close_search_test(frame)

    def test_create_readback_moves_slot_to_populated_selector_and_updates_visible_list(self):
        frame = self.connected_frame()
        try:
            self.seed_search_banks(frame)
            frame.current_list_view = lambda: 'SEARCH BANKS'
            frame.prepare_list((('Bank', 60), ('Lower', 120), ('Upper', 120), ('Step', 90),
                                ('Auto', 45), ('Mode', 55), ('Att', 45), ('Name', 140)))
            frame.open_search_bank(create=True)
            dialog = frame.search_bank_dialog
            frame.set_search_banks('SRB ---')
            dialog.lower.SetValue('5')
            dialog.upper.SetValue('6')
            dialog.name.SetValue('TEST RANGE')
            frame.serial.write.reset_mock()
            dialog.on_save(None)
            self.assertEqual(frame.serial.write.call_args.args[0],
                             b'SEB SL0005000000 SU0006000000 AU1 TTTEST RANGE\r\nSRB\r\n')
            frame.set_search_banks('SRB SL0005000000 SU0006000000 ST005000 AU1 MD2 AT0 TTTEST RANGE')
            self.assertFalse(frame.search_banks['B']['empty'])
            self.assertNotIn('B', frame.search_bank_slots(True))
            self.assertEqual(frame.search_bank_choice.GetStringSelection(), 'TEST RANGE')
            self.assertEqual(frame.search_bank_choice.GetClientData(frame.search_bank_choice.GetSelection()), 'B')
            row = frame.search_bank_rows['B']
            self.assertEqual(frame.edit_list.list.GetItemText(row, 7), 'TEST RANGE')
            self.assertEqual(frame.edit_list.list.GetFirstSelected(), row)
            self.assertFalse(dialog.creating)
            self.assertEqual(dialog.bank.GetItems(), ['TEST RANGE'])
            self.assertEqual(dialog.selected_bank(), 'B')
            self.assertFalse(dialog.bank.IsEnabled())
        finally:
            self.close_search_test(frame)

    def test_shared_group_master_checkbox_changes_proposal_only_for_both_group_kinds(self):
        frame = self.connected_frame()
        try:
            for kind, count in (('scan', 20), ('search', 40)):
                dialog = main.GroupDialog(frame, kind, 1)
                try:
                    self.assertFalse(dialog.select_all.GetValue())
                    self.assertFalse(dialog.select_all.Is3State())
                    self.assertFalse(dialog.select_all.IsEnabled())
                    dialog.show_members(('A', 'B'))
                    self.assertTrue(dialog.select_all.IsEnabled())
                    frame.serial.write.reset_mock()
                    dialog.select_all.SetValue(True)
                    event = wx.CommandEvent(wx.EVT_CHECKBOX.typeId, dialog.select_all.GetId())
                    dialog.select_all.GetEventHandler().ProcessEvent(event)
                    self.assertEqual(dialog.members.GetCheckedItems(), tuple(range(count)))
                    self.assertTrue(dialog.save.IsEnabled())
                    self.assertEqual(dialog.confirmed_members, ('A', 'B'))
                    frame.serial.write.assert_not_called()
                    dialog.select_all.SetValue(False)
                    dialog.select_all.GetEventHandler().ProcessEvent(event)
                    self.assertEqual(dialog.members.GetCheckedItems(), ())
                    self.assertTrue(dialog.save.IsEnabled())
                    frame.serial.write.assert_not_called()
                    dialog.show_members(())
                    dialog.on_select_all(None)
                    self.assertFalse(dialog.save.IsEnabled())
                    dialog.members.Check(0, True)
                    dialog.on_members_changed(None)
                    self.assertFalse(dialog.select_all.GetValue())
                    self.assertTrue(dialog.save.IsEnabled())
                finally:
                    dialog.read_timer.Stop()
                    dialog.Destroy()
        finally:
            frame.alive.clear()
            frame.serial.is_open = False

    def test_group_zero_master_checkbox_stays_read_only_for_both_group_kinds(self):
        frame = self.connected_frame()
        try:
            for kind in ('scan', 'search'):
                dialog = main.GroupDialog(frame, kind, 0)
                try:
                    dialog.show_members(('A',))
                    self.assertFalse(dialog.select_all.IsEnabled())
                    self.assertFalse(dialog.members.IsEnabled())
                    self.assertFalse(dialog.save.IsEnabled())
                    dialog.select_all.SetValue(True)
                    dialog.on_select_all(None)
                    self.assertEqual(dialog.proposed_members(), ('A',))
                    frame.serial.write.assert_not_called()
                finally:
                    dialog.read_timer.Stop()
                    dialog.Destroy()
        finally:
            frame.alive.clear()
            frame.serial.is_open = False

    def test_running_temporary_group_settings_change_and_restore_without_restart(self):
        frame = self.connected_frame()
        fixture = scan_tests.ScanTests()
        original, scan = fixture.prepare()
        scan.controller = frame
        frame.scan_call_later = original.scan_call_later
        frame.temporary_scan = scan
        frame.active_operation = ('Memory Scan', 'Current Bank')
        frame.group_response['scan'] = scan.group
        frame.scan_levels[scan.group] = scan.level
        frame.current_scan_level = scan.level
        frame.level_vfo_context = False
        snapshot = {key: dict(value) if isinstance(value, dict) else value for key, value in scan.saved.items()}
        try:
            frame.update_operation_ui()
            self.assertTrue(frame.GetToolBar().GetToolEnabled(frame.operation_settings_tool_id))
            self.assertFalse(frame.GetToolBar().GetToolEnabled(frame.config_tool_id))
            frame.open_operation_settings(None)
            dialog = frame.settings_dialog
            self.assertEqual((dialog.family, dialog.group), ('X', scan.group))
            self.assertEqual(frame.serial.write.call_args_list[-1].args[0], b'GM\r\n')
            frame.set_group_response('GM%d XD2.0 XB %03d XA 000 XP00 XMF' % (scan.group, scan.level))
            frame.set_group_response(membership('J'))
            changes = {'XA': 24, 'XB': 42, 'XD': 30, 'XM': '1', 'XP': 99}
            for key, value in changes.items():
                dialog.controls[key].SetSelection(dialog.options[key].index(value))
            dialog.on_changed(None)
            self.assertTrue(dialog.save.IsEnabled())
            frame.serial.write.reset_mock()
            dialog.on_save(None)
            writes = [call.args[0] for call in frame.serial.write.call_args_list]
            self.assertEqual(writes, [b'XD30\r\nXP99\r\nXB042\r\nXA024\r\nXM1\r\n', b'GM\r\n'])
            frame.set_group_response('GM2 XD3.0 XB 042 XA 024 XP99 XM1')
            frame.set_group_response(membership('J'))
            self.assertEqual(frame.sql.GetValue(), 42)
            self.assertEqual(frame.memory_bank_dwell[scan.target], 99)
            self.assertEqual(scan.saved, snapshot)
            self.assertEqual(scan.phase, 'active')
            self.assertEqual(frame.active_operation, ('Memory Scan', 'Current Bank'))
            frame.serial.write.reset_mock()
            frame.on_operation_stop(None)
            wx.Yield()
            scan_tests.drain_pacing(scan)
            restore = [call.args[0] for call in frame.serial.write.call_args_list]
            self.assertEqual(restore[-6:-1], [b'XA023\r\n', b'XB009\r\n', b'XD20\r\n', b'XM2\r\n', b'XP60\r\n'])
            scan.receive(header(2))
            scan.receive(membership(''))
            scan_tests.drain_pacing(scan)
            scan.receive(header(4))
            scan.receive(membership('BCD'))
            self.assertIsNone(frame.temporary_scan)
        finally:
            if frame.settings_dialog is not None:
                frame.settings_dialog.Destroy()
                frame.settings_dialog = None
            frame.temporary_scan = None
            frame.alive.clear()
            frame.serial.is_open = False

    def test_running_linked_context_overrides_visible_operation_and_serial_config_guard(self):
        frame = self.connected_frame()
        try:
            for operation, group, kind, family in ((('Memory Scan', 'Scan Group'), 3, 'scan', 'X'),
                                                   (('Frequency Search', 'Search Group'), 4, 'search', 'S'),
                                                   (('Frequency Search', 'Range'), None, 'search', 'D')):
                frame.active_operation = operation
                frame.group_response[kind] = group
                frame.update_operation_ui()
                self.assertTrue(frame.GetToolBar().GetToolEnabled(frame.operation_settings_tool_id))
                self.assertFalse(frame.GetToolBar().GetToolEnabled(frame.config_tool_id))
                self.assertEqual(frame.operation_settings_context(), (family, group))
                with patch.object(main, 'OperationSettingsDialog') as create:
                    frame.open_operation_settings(None)
                    create.assert_called_once_with(frame, family, group)
                    create.return_value.read.assert_called_once_with()
                frame.settings_dialog = None
                with patch.object(frame, 'stop_thread') as stop, patch.object(frame, 'close_serial') as close:
                    frame.set_port()
                stop.assert_not_called()
                close.assert_not_called()
                self.assertEqual(frame.aor_status.GetStatusText(), 'Stop the current operation before changing Serial Config.')
            frame.active_operation = None
            frame.temporary_scan = Mock(phase='original')
            frame.update_operation_ui()
            self.assertFalse(frame.GetToolBar().GetToolEnabled(frame.operation_settings_tool_id))
            self.assertFalse(frame.GetToolBar().GetToolEnabled(frame.config_tool_id))
            with patch.object(frame, 'restore_temporary_scan') as restore:
                frame.set_port()
            restore.assert_not_called()
            frame.temporary_scan = None
            frame.update_operation_ui()
            self.assertTrue(frame.GetToolBar().GetToolEnabled(frame.config_tool_id))
        finally:
            frame.temporary_scan = None
            frame.settings_dialog = None
            frame.alive.clear()
            frame.serial.is_open = False

    def test_running_linked_group_save_uses_parameters_without_selecting_or_restarting(self):
        frame = self.connected_frame()
        try:
            for operation, kind, family, group in ((('Memory Scan', 'Scan Group'), 'scan', 'X', 3),
                                                    (('Frequency Search', 'Search Group'), 'search', 'S', 4)):
                frame.active_operation = operation
                frame.group_response[kind] = group
                dialog = main.OperationSettingsDialog(frame, family, group)
                frame.settings_dialog = dialog
                frame.serial.write.reset_mock()
                dialog.read()
                self.assertEqual(frame.serial.write.call_args.args[0], ('G%s\r\n' % ('M' if family == 'X' else 'S')).encode('ascii'))
                dialog.accept_context(kind, group)
                values = {family + 'A': 0, family + 'B': 10, family + 'D': 20, family + 'P': 5}
                if family == 'X':
                    values['XM'] = 'F'
                for key, value in values.items():
                    dialog.accept(key, value)
                dialog.controls[family + 'P'].SetSelection(6)
                dialog.on_changed(None)
                frame.serial.write.reset_mock()
                dialog.on_save(None)
                self.assertEqual([c.args[0] for c in frame.serial.write.call_args_list],
                                 [(family + 'P06\r\n').encode('ascii'),
                                  ('G%s\r\n' % ('M' if family == 'X' else 'S')).encode('ascii')])
                self.assertEqual(frame.active_operation, operation)
                dialog.Destroy()
                frame.settings_dialog = None
        finally:
            if frame.settings_dialog is not None:
                frame.settings_dialog.Destroy()
                frame.settings_dialog = None
            frame.alive.clear()
            frame.serial.is_open = False


if __name__ == '__main__':
    unittest.main()
