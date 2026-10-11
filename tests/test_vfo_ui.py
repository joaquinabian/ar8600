"""Focused VFO checks; no scanner connection or application auto-connect."""
from decimal import Decimal
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock

from test_current_bank_scan import Controller, main
import test_current_bank_scan as scan_tests
from test_database_console import controller, RECORD
from aor_control_frame import AorCtrlFrame
from aor_functions import display_vfo_frequency
import wx


class VfoTests(unittest.TestCase):
    def test_display_units_and_significant_precision(self):
        for hz, label in ((500000, '500.0 kHz'), (1620000, '1620.0 kHz'),
                          (93500000, '93.500 MHz'), (118025000, '118.025 MHz'),
                          (118033330, '118.03333 MHz'), (1296000000, '1.296000 GHz')):
            self.assertEqual(display_vfo_frequency(hz), label)
        self.assertEqual(display_vfo_frequency(Decimal('118033330')), '118.03333 MHz')

    def listening_controller(self):
        ctrl = controller()
        ctrl.rb_vfos = Mock()
        ctrl.rb_vfos.GetSelection.return_value = 0  # last RX can differ from preference
        ctrl.listening_vfo = 2
        ctrl.normal_vfo_command = MethodType(main.AorCtrl.normal_vfo_command, ctrl)
        ctrl.enter_vfo_level_context = Mock()
        ctrl.show_now_receiving = Mock()
        return ctrl

    def test_database_uses_listening_default_and_explicit_auxiliary_choice(self):
        ctrl = self.listening_controller()
        ctrl.tune_database_record(RECORD)
        self.assertTrue(ctrl.commands[0].startswith(b'MC1\r\nVF\r\nAU0\r\n'))
        ctrl.commands.clear()
        ctrl.listening_vfo = 1
        ctrl.tune_database_record(RECORD)
        self.assertTrue(ctrl.commands[0].startswith(b'MC1\r\nVB\r\nAU0\r\n'))

    def test_manual_tune_selects_listening_vfo_then_one_rx(self):
        ctrl = self.listening_controller()
        ctrl.tuning_panel = Mock(freq=104.0)
        main.AorCtrl.on_enter_freq(ctrl, None)
        self.assertEqual(ctrl.commands, [b'VF\r\nRF0104.00000\r\n', b'RX\r\n'])
        ctrl.commands.clear()
        ctrl.listening_vfo = 0
        main.AorCtrl.on_enter_freq(ctrl, None)
        self.assertEqual(ctrl.commands[0], b'VA\r\nRF0104.00000\r\n')

    def test_range_search_uses_ab_and_restores_listening_vfo(self):
        ctrl = self.listening_controller()
        ctrl.search_restore_command = None
        ctrl.vfo_status = {}
        ctrl.range_mode = Mock()
        ctrl.range_mode.GetSelection.return_value = 0
        ctrl.range_lower = Mock()
        ctrl.range_lower.GetValue.return_value = '93'
        ctrl.range_upper = Mock()
        ctrl.range_upper.GetValue.return_value = '94'
        ctrl.range_step = Mock()
        ctrl.range_step.GetValue.return_value = '100'
        self.assertTrue(main.AorCtrl.on_vfo_start(ctrl, None))
        self.assertTrue(ctrl.commands[0].startswith(b'VA\r\nRF0093000000\r\nVB\r\n'))
        self.assertIn(b'VS\r\nRX\r\n', ctrl.commands[0])
        self.assertEqual(ctrl.search_restore_command, b'VF\r\n')
        self.assertEqual(ctrl.listening_vfo, 2)

    def test_cached_vfos_are_independent_and_scan_does_not_select_a(self):
        ctrl = self.listening_controller()
        ctrl.vfa = ctrl.vfb = ctrl.vfo = None
        ctrl.vfo_status = {}
        ctrl.lb_vfa, ctrl.lb_vfb, ctrl.lb_vfo = Mock(), Mock(), Mock()
        ctrl.cbx_step = Mock()
        ctrl.cbx_step.GetItems.return_value = ['100']
        ctrl.ckbx_auto, ctrl.ckbx_att = Mock(), Mock()
        for index, prefix, frequency in ((0, 'VA', '0093500000'),
                                          (1, 'VB', '0145500000'), (2, 'VF', '0001620000')):
            main.AorCtrl.set_vfo_text(ctrl, prefix + ' RF' + frequency + ' ST100000 AU0 MD0 AT0', index)
        self.assertEqual((ctrl.vfa, ctrl.vfb, ctrl.vfo), ('0093500000', '0145500000', '0001620000'))
        self.assertEqual(set(ctrl.vfo_status), {0, 1, 2})
        ctrl.active_operation = ('Frequency Search', 'Range')
        ctrl.rb_vfos.SetSelection.reset_mock()
        main.AorCtrl.set_vfo_text(ctrl, 'VA RF0094000000 ST100000 AU0 MD0 AT0', 0)
        ctrl.rb_vfos.SetSelection.assert_not_called()
        self.assertEqual(ctrl.listening_vfo, 2)


class VfoLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App(False)

    def test_bordered_groups_aligned_units_and_prominent_single_vfo(self):
        frame = AorCtrlFrame(None)
        try:
            frame.Show()
            wx.Yield()
            selector = frame.rb_vfos
            self.assertEqual(selector.GetStringSelection(), '1-VFO')
            self.assertEqual([button.GetLabel() for button in selector.buttons.values()],
                             ['1-VFO', 'VFO-A', 'VFO-B'])
            self.assertEqual(len(selector.boxes), 2)
            self.assertTrue(all(isinstance(box, wx.StaticBox) for box in selector.boxes))
            self.assertTrue(all(box.GetLabel() == '' for box in selector.boxes))
            self.assertFalse(any(isinstance(child, wx.StaticLine) for child in selector.GetChildren()))
            self.assertGreater(frame.lb_vfo.GetFont().GetPointSize(), frame.lb_vfa.GetFont().GetPointSize())
            self.assertLess(frame.lb_vfo.GetScreenPosition().y, frame.lb_vfa.GetScreenPosition().y)
            displays = (frame.lb_vfo, frame.lb_vfa, frame.lb_vfb)
            for display, hz in zip(displays, (93500000, 1620000, 1296000000)):
                display.SetLabel(display_vfo_frequency(hz))
            selector.Layout()
            wx.Yield()
            self.assertEqual([display.unit.GetLabel() for display in displays], ['MHz', 'kHz', 'GHz'])
            self.assertEqual([display.value.GetLabel() for display in displays], ['93.500', '1620.0', '1.296000'])
            self.assertEqual(len({display.unit.GetScreenPosition().x for display in displays}), 1)
            self.assertEqual(len({display.unit.GetSize().width for display in displays}), 1)
            self.assertEqual(len({display.unit.GetFont().GetPointSize() for display in displays}), 1)
            self.assertTrue(all(display.value.GetWindowStyleFlag() & wx.ALIGN_RIGHT for display in displays))
            self.assertTrue(all(not display.unit.GetWindowStyleFlag() & wx.ALIGN_RIGHT for display in displays))
            frame.SetSize((frame.GetSize().width + 180, frame.GetSize().height))
            frame.Layout()
            wx.Yield()
            self.assertEqual(len({display.unit.GetScreenPosition().x for display in displays}), 1)
            events = []
            selector.Bind(wx.EVT_RADIOBOX, lambda event: events.append(event.GetInt()))
            selector.SetSelection(1)
            selector.on_select(1)
            self.assertEqual(events, [1])
            self.assertEqual(selector.GetStringSelection(), 'VFO-B')
        finally:
            frame.Destroy()
            wx.Yield()


class StartupTests(unittest.TestCase):
    def startup_controller(self):
        ctrl = scan_tests.GuiBoundaryTests().level_controller()
        ctrl.connected = False
        ctrl.level_squelch_value = None
        ctrl.receiver_status = {'context': 'VFO'}
        ctrl.vfa = ctrl.vfb = ctrl.vfo = None
        ctrl.vfo_status = {}
        ctrl.background_vfo = None
        ctrl.rb_vfos = Mock()
        ctrl.lb_vfa, ctrl.lb_vfb, ctrl.lb_vfo = Mock(), Mock(), Mock()
        ctrl.cbx_step = Mock()
        ctrl.cbx_step.GetItems.return_value = ['12.5', '100']
        ctrl.ckbx_auto, ctrl.cbx_mode, ctrl.ckbx_att = Mock(), Mock(), Mock()
        ctrl.update_receiver_switch_ui = Mock()
        ctrl.monitor_muted = False
        ctrl.bandscope = None
        ctrl.receive_context_status = Mock()
        ctrl.receive_memory_delete_status = ctrl.on_memory_flag_status = Mock()
        ctrl.maybe_load_initial_bank = Mock()
        ctrl.mirror_console_line = Mock()
        ctrl.startup_connect_pending = False
        ctrl.cancel_startup_connect = Mock()
        ctrl.receiver_switch_pending = set()
        ctrl.lm_timer = Mock()
        ctrl.lm_timer.IsRunning.return_value = False
        ctrl.operation_selection = lambda: ('Memory Scan', 'Current Bank')
        for method in ('initialize_receiver', 'receive_initial_vfo', 'set_vfo_text', 'start_monitoring'):
            setattr(ctrl, method, MethodType(getattr(main.AorCtrl, method), ctrl))
        ctrl.initialize_receiver()
        return ctrl

    def test_all_vfos_preloaded_without_exposing_intermediate_contexts(self):
        ctrl = self.startup_controller()
        ctrl.rb_vfos.SetSelection.assert_called_once_with(2)
        samples = ('VF RF0093500000 ST100000 AU1 MD0 AT1',
                   'VA RF0197300000 ST012500 AU1 MD1 AT0',
                   'VB RF0145500000 ST012500 AU0 MD1 AT0',
                   'VF RF0093500000 ST100000 AU1 MD0 AT1')
        for line in samples[:3]:
            main.AorCtrl.on_serial_read(ctrl, SimpleNamespace(data=[line]))
            ctrl.receive_context_status.assert_not_called()
            ctrl.show_now_receiving.assert_not_called()
            ctrl.rb_vfos.SetSelection.assert_called_once_with(2)
            self.assertFalse(ctrl.connected)
        main.AorCtrl.on_serial_read(ctrl, SimpleNamespace(data=[samples[3]]))
        self.assertTrue(ctrl.connected)
        self.assertFalse(ctrl.vfo_initialization_pending)
        self.assertIsNone(ctrl.background_vfo)
        self.assertEqual((ctrl.vfo, ctrl.vfa, ctrl.vfb), ('0093500000', '0197300000', '0145500000'))
        self.assertEqual(set(ctrl.vfo_status), {0, 1, 2})
        ctrl.lb_vfo.SetLabel.assert_called_with('93.500 MHz')
        ctrl.lb_vfa.SetLabel.assert_called_with('197.300 MHz')
        ctrl.lb_vfb.SetLabel.assert_called_with('145.500 MHz')
        ctrl.rb_vfos.SetSelection.assert_called_with(2)
        ctrl.receive_context_status.assert_called_once_with(samples[3])
        self.assertEqual(ctrl.commands[:6], [b'VF\r\nRX\r\n', b'VA\r\nRX\r\n',
                                           b'VB\r\nRX\r\n', b'VF\r\nRX\r\n',
                                           b'TB\r\n', b'TB\r\n'])
        self.assertEqual(ctrl.commands[6:], [b'DB\r\n', b'NL\r\nAF\r\n', b'LC1\r\n'])
        self.assertEqual(ctrl.level_context(), ('DB', None))
        self.assertTrue(ctrl.level_squelch_pending)
        ctrl.sql.Enable.assert_called_with(False)
        main.AorCtrl.on_serial_read(ctrl, SimpleNamespace(data=['DB023']))
        self.assertFalse(ctrl.level_squelch_pending)
        self.assertEqual(ctrl.level_squelch_value, 23)
        ctrl.sql.Enable.assert_called_with(True)
        ctrl.sql_value.SetLabel.assert_called_with('23')

    def test_idle_current_bank_cannot_force_xb(self):
        ctrl = self.startup_controller()
        ctrl.connected = True
        ctrl.level_vfo_context = False
        ctrl.commands.clear()
        ctrl.refresh_level_context()
        self.assertEqual(ctrl.level_context(), ('DB', None))
        self.assertEqual(ctrl.commands, [b'DB\r\n'])
        ctrl.accept_operation_parameter('DB057')
        ctrl.sql.Enable.assert_called_with(True)
        ctrl.temporary_scan = SimpleNamespace(group=2, busy=False)
        ctrl.group_response['scan'] = 2
        ctrl.scan_levels[2] = 19
        self.assertEqual(ctrl.level_context(), ('XB', 2))

    def test_idle_linked_selection_without_prepared_group_remains_db(self):
        ctrl = self.startup_controller()
        ctrl.level_vfo_context = False
        ctrl.operation_selection = lambda: ('Memory Scan', 'Scan Group')
        ctrl.cbx_scan.GetValue.return_value = '3'
        self.assertEqual(ctrl.level_context(), ('DB', None))
        ctrl.group_loading['scan'] = 3  # Explicit group preparation/read.
        self.assertEqual(ctrl.level_context(), ('XB', 3))

    def test_active_scan_reads_group_level_then_returns_to_db(self):
        ctrl = self.startup_controller()
        ctrl.connected = True
        ctrl.level_vfo_context = False
        ctrl.operation_selection = lambda: ('Memory Scan', 'Scan Group')
        ctrl.active_operation = ('Memory Scan', 'Scan Group')
        ctrl.cbx_scan.GetValue.return_value = '3'
        ctrl.commands.clear()
        ctrl.refresh_level_context()
        self.assertEqual(ctrl.commands, [b'GM\r\n'])
        main.AorCtrl.set_group_response(ctrl, 'GM3 XB023 XA000 XD2.0 XM0 XP00')
        self.assertEqual(ctrl.level_context(), ('XB', 3))
        ctrl.sql.Enable.assert_called_with(True)
        ctrl.sql_value.SetLabel.assert_called_with('23')
        ctrl.active_operation = None
        ctrl.enter_vfo_level_context()
        self.assertEqual(ctrl.commands, [b'GM\r\n', b'DB\r\n'])
        self.assertEqual(ctrl.level_context(), ('DB', None))


if __name__ == '__main__':
    unittest.main()
