"""Focused scan controls tests; no scanner opens or hardware writes."""
from decimal import Decimal
from types import MethodType
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
    for name in ('memory_scan_paused', 'paused_scan_target', 'on_scan_resume', 'on_scan_to_vfo',
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
        self.assertIsNone(ctrl.paused_scan_target())
        ctrl.on_scan_to_vfo(None)
        self.assertIsNone(ctrl.pending_vfo_target)
        ctrl.operation_signal_open = False
        self.assertFalse(ctrl.memory_scan_paused())

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
        scan.receive(header(2, dwell=99))
        scan.receive(membership(''))
        self.assertFalse(any(b'VF\r\n' in c for c in ctrl.commands))
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
            dialog = main.OperationSettingsDialog(frame, 'X', 1)
            self.assertEqual(dialog.options['XP'], list(range(100)))
            self.assertEqual(dialog.controls['XP'].GetString(99), '99 s')
        finally:
            if dialog is not None:
                dialog.Destroy()
            frame.Destroy()
            wx.Yield()


class GuiActionTests(unittest.TestCase):
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
                          'Include in Selected Channels', 'Skip during Stored Channels scanning'])
        self.assertTrue(menu[0][1])
        frame.open_memory_channel.assert_called_once_with()
        frame.memory_rows.clear()
        frame.edit_list.memory_model.channels.clear()
        self.assertEqual(self.capture_menu(), [('New Channel...', True)])
        frame.open_memory_channel.assert_called_once_with()

    def test_new_disabled_without_empty_slot_or_edit_permission(self):
        frame = self.frame
        frame.selected_memory_bank = lambda: {'bank': 'A', 'channels': 1}
        frame.memory_channel_available = lambda: True
        frame.memory_empty_channels.clear()
        self.assertEqual(self.capture_menu(), [('New Channel...', False)])
        frame.memory_empty_channels.add('A00')
        frame.memory_channel_available = lambda: False
        self.assertEqual(self.capture_menu(), [('New Channel...', False)])
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


if __name__ == '__main__':
    unittest.main()
