"""Local marker/gesture regressions; fake serial, no receiver hardware."""
import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_bandscope_statistic import Window, STATUS, RECEIVER, main


def event(x, y):
    return SimpleNamespace(GetPosition=lambda: (x, y))


def double_click(window, x=500, y=150, jitter=0):
    # Native wx sequence: DOWN, UP, DCLICK, UP. UP must not move the marker again.
    window.on_plot_left_down(event(x - jitter, y - jitter))
    window.on_plot_motion(event(x, y))
    window.on_plot_left_up(event(x, y))
    window.on_plot_double_click(event(x, y))
    window.on_plot_left_up(event(x + jitter, y + jitter))


class MarkerTests(unittest.TestCase):
    def test_double_click_once_with_jitter_no_capture_no_zoom_or_serial(self):
        window = Window()
        window.view.reset(window.status, False)
        before = vars(window.view).copy()
        window.on_marker_click = Mock(wraps=window.on_marker_click)
        double_click(window, jitter=3)
        window.on_marker_click.assert_called_once()
        self.assertNotEqual(window.local_marker_hz, 93500000)
        self.assertEqual(vars(window.view), before)
        self.assertIsNone(window.drag_start)
        self.assertIsNone(window.drag_end)
        self.assertFalse(window.drag_moved)
        window.plot.CaptureMouse.assert_not_called()
        self.assertEqual(window.commands, [])
        self.assertIsNone(window.waiting)
        self.assertFalse(window.running)

    def test_marker_is_local_in_every_run_mode_and_all_wait_states(self):
        for mode in Window().SWEEP_MODES:
            for waiting in ('data', 'status', 'mode_peak', 'receiver', 'restore', None):
                with self.subTest(mode=mode, waiting=waiting):
                    window = Window()
                    window.select_mode(mode)
                    window.start_sweep(peak=mode == 'Peak Hold')
                    window.waiting = waiting
                    window.statistic.add_sweep((5,) * 128, window.status)
                    window.spectrum.add_sweep((5,) * 128, window.status)
                    window.samples = (5,) * 128
                    window.trace_status = dict(window.status)
                    before = deepcopy((window.status, window.samples, window.trace_status,
                                       vars(window.statistic), vars(window.spectrum), vars(window.view),
                                       window.receiver_state.known, window.running))
                    config = window.run_config
                    window.commands.clear()
                    window.timer.reset_mock()
                    window.on_start = Mock(side_effect=AssertionError('Marker called Start'))
                    window.request_sweep = Mock(side_effect=AssertionError('Marker requested a sweep'))
                    window.request_status = Mock(side_effect=AssertionError('Marker queried status'))
                    double_click(window)
                    self.assertEqual(window.commands, [])
                    self.assertIs(window.run_config, config)
                    self.assertEqual(window.waiting, waiting)
                    self.assertEqual((window.status, window.samples, window.trace_status,
                                      vars(window.statistic), vars(window.spectrum), vars(window.view),
                                      window.receiver_state.known, window.running), before)
                    window.timer.StartOnce.assert_not_called()
                    window.timer.Stop.assert_not_called()
                    self.assertNotIn('Reading bandscope status', window.message.label)

    def test_marker_available_before_first_sweep_and_offline(self):
        window = Window()
        window.status = None
        window.entered = False
        window.controller.connected = False
        window.centre_input.GetValue.return_value = '27.2'
        window.span_choice.SetSelection(4)
        window.on_centre(None)
        double_click(window)
        self.assertTrue(26950000 <= window.local_marker_hz <= 27450000)
        self.assertEqual(window.commands, [])
        self.assertIsNone(window.run_config)

    def test_zoomed_frequency_mapping_and_hardware_status_does_not_replace_local_marker(self):
        window = Window()
        window.view.reset(window.status, False)
        window.view.frequency_low, window.view.frequency_high = 93500000, 93520000
        double_click(window, 417, 150)
        self.assertEqual(window.local_marker_hz, 93510000)
        self.assertIn('93.510000 MHz', window.info.SetLabel.call_args.args[0])
        window.start_sweep()
        self.assertEqual(window.local_marker_hz, 93510000)
        self.assertFalse(any(line.startswith(b'MF') for data in window.commands for line in data.splitlines()))

    def test_tune_to_local_marker_then_further_clicks_cannot_reenter_analyser(self):
        window = Window()
        window.start_sweep()
        window.finish_sweep()
        window.capture_receiver(RECEIVER)
        window.view.zoom((200, 100), (600, 250), window.plot_rect())
        double_click(window, 417, 150)
        marker = window.local_marker_hz
        before = deepcopy((window.samples, window.trace_status, vars(window.view), window.status))
        window.commands.clear()
        window.on_tune_marker(None)
        self.assertEqual(window.commands, [('VF\r\nRF%010d\r\nRX\r\n' % marker).encode('ascii')])
        window.capture_receiver(RECEIVER)
        self.assertEqual((window.samples, window.trace_status, vars(window.view), window.status), before)
        window.commands.clear()
        window.timer.reset_mock()
        for x in (200, 400, 600):
            double_click(window, x, 150)
        self.assertEqual(window.commands, [])
        self.assertFalse(window.running)
        self.assertFalse(window.entered)
        self.assertIsNone(window.waiting)
        self.assertNotEqual(window.local_marker_hz, marker)
        self.assertEqual((window.samples, window.trace_status, vars(window.view), window.status), before)
        window.timer.StartOnce.assert_not_called()
        window.on_start(None)
        self.assertEqual(window.commands, [b'RX\r\n'])
        window.capture_receiver(RECEIVER)
        self.assertIn(b'AM\r\nAM\r\n', window.commands)

    def test_tune_during_dump_drains_then_uses_captured_local_marker(self):
        window = Window()
        window.select_mode('Loop')
        window.start_sweep()
        double_click(window)
        marker = window.local_marker_hz
        window.commands.clear()
        window.on_tune_marker(None)
        self.assertEqual(window.commands, [])
        double_click(window, 650, 150)  # Does not change the already requested explicit tune.
        window.finish_sweep()
        self.assertEqual(window.commands, [('VF\r\nRF%010d\r\nRX\r\n' % marker).encode('ascii')])
        self.assertFalse(window.running)

    def test_genuine_drag_captures_and_zooms_but_does_not_move_marker(self):
        window = Window()
        marker = window.marker_frequency()
        window.view.reset(window.status, False)
        window.on_plot_left_down(event(200, 100))
        window.plot.CaptureMouse.assert_not_called()
        window.on_plot_motion(event(600, 250))
        window.plot.CaptureMouse.assert_called_once()
        window.plot.HasCapture.return_value = True
        window.on_plot_left_up(event(600, 250))
        self.assertGreater(window.view.frequency_low, 93450000)
        self.assertLess(window.view.frequency_high, 93550000)
        self.assertEqual(window.marker_frequency(), marker)
        window.plot.ReleaseMouse.assert_called_once()
        window.on_reset_zoom(None)
        self.assertEqual((window.view.frequency_low, window.view.frequency_high), (93450000, 93550000))
        self.assertEqual(window.commands, [])

    def test_double_click_cancels_any_drag_rectangle_and_capture(self):
        window = Window()
        window.view.reset(window.status, False)
        before = vars(window.view).copy()
        window.on_plot_left_down(event(200, 100))
        window.on_plot_motion(event(600, 250))
        window.plot.HasCapture.return_value = True
        window.on_plot_double_click(event(500, 150))
        window.on_plot_left_up(event(500, 150))
        self.assertEqual(vars(window.view), before)
        self.assertIsNone(window.drag_start)
        self.assertFalse(window.drag_moved)
        window.plot.ReleaseMouse.assert_called_once()
        self.assertEqual(window.commands, [])

    def test_paint_renders_without_serial_queries_parsing_or_waits(self):
        window = Window()
        window.samples = (5,) * 128
        window.trace_status = dict(window.status)
        window.marker_frequency()
        dc = Mock()
        dc.GetTextExtent.return_value = (100, 14)
        window.controller.write_serial = Mock(side_effect=AssertionError('Paint wrote serial'))
        window.request_status = Mock(side_effect=AssertionError('Paint queried status'))
        clip = Mock(__enter__=Mock(), __exit__=Mock(return_value=False))
        with patch.object(main.wx, 'AutoBufferedPaintDC', return_value=dc), \
                patch.object(main.wx, 'Pen'), patch.object(main.wx, 'Brush'), \
                patch.object(main.wx, 'DCClipper', return_value=clip):
            window.on_paint(None)
        window.controller.write_serial.assert_not_called()
        # Guard the two render functions against future direct serial/wait additions.
        tree = ast.parse(Path(main.__file__).read_text(encoding='utf-8'))
        scope = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'BandscopeWindow')
        for method in scope.body:
            if isinstance(method, ast.FunctionDef) and method.name in ('on_paint', 'draw_trace'):
                calls = [node.func.attr for node in ast.walk(method)
                         if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
                self.assertFalse(set(calls) & {'read', 'read_until', 'write', 'write_serial', 'send',
                                               'request_status', 'sleep', 'wait', 'add_line'})


if __name__ == '__main__':
    unittest.main()
