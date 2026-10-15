"""Focused Spectrum Average tests with fake serial; never use scanner hardware."""
import contextlib
import io
import sys
import unittest
from copy import deepcopy

GUI = '--gui' in sys.argv
if GUI:
    sys.argv.remove('--gui')

import test_bandscope_statistic as scope
from aor_functions import BandscopeAverage, BANDSCOPE_AVERAGE_WINDOWS, parse_bandscope_status


STATUS = parse_bandscope_status(scope.STATUS)


def samples(*levels):
    return tuple(levels) + (0,) * (128 - len(levels))


class AverageTests(unittest.TestCase):
    def test_window_one_is_latest_and_invalid_bins_have_no_value(self):
        average = BandscopeAverage(1)
        average.add_sweep(samples(2, 4, 0, 1), STATUS)
        self.assertEqual(average.add_sweep(samples(8, 1, 15, 0), STATUS)[:4], (8, None, 15, None))
        self.assertEqual(len(average.history), 1)
        self.assertEqual(average.valid[:4], [1, 0, 1, 0])

    def test_rolling_means_and_eviction_exclude_invalid_values(self):
        average = BandscopeAverage(2)
        average.add_sweep(samples(2, 0, 1, 5), STATUS)
        result = average.add_sweep(samples(4, 8, 0, 10), STATUS)
        self.assertEqual(result[:4], (3, 8, None, 7.5))
        result = average.add_sweep(samples(8, 1, 0, 0), STATUS)
        self.assertEqual(result[:4], (6, 8, None, 10))
        self.assertEqual(average.sums[:4], [12, 8, 0, 10])
        self.assertEqual(average.valid[:4], [2, 1, 0, 1])
        average.add_sweep(samples(0, 0, 0, 0), STATUS)
        self.assertEqual(average.averages()[:4], (8, None, None, None))

    def test_relative_uses_full_spectrum_and_maximum_is_exactly_100(self):
        average = BandscopeAverage()
        average.add_sweep(samples(3, 6, 12), STATUS)
        self.assertEqual(average.averages(True)[:4], (25, 50, 100, None))
        average.add_sweep(samples(4, 7, 13), STATUS)
        self.assertEqual(max(value for value in average.averages(True) if value is not None), 100)
        self.assertTrue(all(value is None or 0 <= value <= 100 for value in average.averages(True)))

    def test_no_valid_measurements_has_no_trace(self):
        average = BandscopeAverage()
        average.add_sweep((0, 1) * 64, STATUS)
        self.assertEqual(average.averages(), (None,) * 128)
        self.assertEqual(average.averages(True), (None,) * 128)

    def test_resets_on_window_geometry_or_sample_count_not_marker(self):
        average = BandscopeAverage()
        status = STATUS.copy()
        average.add_sweep((5,) * 128, status)
        status['marker_hz'] += 2000
        average.add_sweep((6,) * 128, status)
        self.assertEqual(len(average.history), 2)
        for field, value in (('centre_hz', 94000000), ('span_code', 6)):
            status[field] = value
            average.add_sweep((7,) * 128, status)
            self.assertEqual(len(average.history), 1)
        average.add_sweep((8,) * 1024, status)
        self.assertEqual(len(average.history), 1)
        self.assertEqual(len(average.sums), 1024)
        for window in BANDSCOPE_AVERAGE_WINDOWS:
            average.set_window(window)
            self.assertEqual(len(average.history), 0)
            self.assertEqual(average.averages(), ())

    def test_malformed_sweep_does_not_mutate_and_history_is_bounded(self):
        average = BandscopeAverage(5)
        for _ in range(20):
            average.add_sweep((15,) * 128, STATUS)
        before = deepcopy(vars(average))
        for bad in ((15,) * 127, (16,) * 128, (1.5,) * 128):
            with self.assertRaises(ValueError):
                average.add_sweep(bad, STATUS)
            self.assertEqual(vars(average), before)
        self.assertEqual(len(average.history), 5)
        self.assertEqual(average.sums, [75] * 128)


class IntegrationTests(unittest.TestCase):
    def window(self):
        window = scope.Window()
        window.select_mode('Average')
        return window

    def test_exclusive_modes_and_continuous_without_loop(self):
        window = scope.Window()
        window.enable_statistic()
        window.select_mode('Average')
        self.assertEqual(window.sweep_mode, 'Average')
        self.assertFalse(hasattr(window, 'loop_checkbox'))
        self.assertTrue(window.average_window.enabled)
        self.assertFalse(window.threshold.enabled)
        window.start_sweep()
        window.finish_sweep('8')
        self.assertEqual(len(window.spectrum.history), 1)
        self.assertEqual(window.statistic.sweeps, 0)
        window.timer.StartOnce.assert_called_with(500)
        window.select_mode('Statistic')
        self.assertEqual(window.sweep_mode, 'Statistic')
        self.assertFalse(window.average_window.enabled)

    def test_peak_hold_off_verified_and_stop_retains_average(self):
        window = self.window()
        window.start_sweep(peak=True)
        self.assertEqual(window.commands[-1], b'PH0\r\nPH\r\n')
        self.assertNotIn(b'DS\r\n', window.commands)
        window.set_peak_hold('PH0')
        window.on_stop(None)
        window.finish_sweep('A')
        self.assertEqual(window.commands[-1], b'VF\r\nRX\r\n')
        self.assertEqual(window.spectrum.averages(), (10,) * 128)
        window.capture_receiver(scope.RECEIVER)
        window.start_sweep(peak=True)
        window.set_peak_hold('PH0')
        window.finish_sweep('6')
        self.assertEqual(len(window.spectrum.history), 2)
        self.assertEqual(window.spectrum.averages(), (8,) * 128)

    def test_scale_and_zoom_do_not_clear_or_renormalize(self):
        window = self.window()
        window.spectrum.add_sweep((0,) * 64 + (3, 6, 12) + (0,) * 61, window.status)
        expected = window.spectrum.averages(True)
        before = deepcopy(vars(window.spectrum))
        window.on_plot_left_down(scope.ZoomTests.event(400, 100))
        window.on_plot_left_up(scope.ZoomTests.event(438, 250))
        low, high = window.view.frequency_low, window.view.frequency_high
        self.assertLess(high, 93504000)  # Global maximum bin is outside the viewport.
        self.assertEqual(window.spectrum.averages(True)[64:67], (25, 50, 100))
        window.spectrum_scale.SetSelection(1)
        window.on_spectrum_scale(None)
        self.assertEqual((window.view.y_low, window.view.y_high), (0, 15))
        window.spectrum_scale.SetSelection(0)
        window.on_spectrum_scale(None)
        self.assertEqual((window.view.y_low, window.view.y_high), (0, 100))
        self.assertEqual((window.view.frequency_low, window.view.frequency_high), (low, high))
        self.assertEqual(vars(window.spectrum), before)
        self.assertEqual(window.spectrum.averages(True), expected)
        self.assertEqual(window.commands, [])

    def test_queued_single_does_not_stop_active_average(self):
        window = self.window()
        window.start_sweep()
        window.select_mode('Single', hardware=True)
        window.finish_sweep()
        self.assertTrue(window.running)
        self.assertEqual(window.processing_mode(), 'Average')
        window.on_stop(None)
        window.capture_receiver(scope.RECEIVER)
        window.start_sweep()
        window.finish_sweep()
        self.assertEqual(window.commands[-1], b'VF\r\nRX\r\n')


    def test_clear_and_window_change_only_clear_accumulation(self):
        window = self.window()
        window.start_sweep()
        window.finish_sweep()
        commands = list(window.commands)
        status = window.status.copy()
        window.on_clear(None)
        self.assertTrue(window.running)
        self.assertEqual(window.spectrum.averages(), ())
        self.assertEqual(window.spectrum.sums, [])
        window.average_window.SetSelection(4)
        window.on_average_window(None)
        self.assertEqual(window.spectrum.window, 10)
        self.assertEqual(window.average_window.GetSelection(), 4)
        self.assertEqual(window.status, status)
        self.assertEqual(window.commands, commands)

    def test_window_change_during_run_is_applied_only_on_next_start(self):
        window = self.window()
        window.start_sweep()
        window.average_window.SetSelection(2)
        before = list(window.commands)
        window.on_average_window(None)
        self.assertEqual(window.commands, before)
        window.finish_sweep()
        self.assertEqual(window.spectrum.window, 10)
        self.assertEqual(len(window.spectrum.history), 1)
        window.on_stop(None)
        window.capture_receiver(scope.RECEIVER)
        window.start_sweep()
        self.assertEqual(window.spectrum.window, 5)
        self.assertEqual(len(window.spectrum.history), 0)


    def test_centre_span_reset_but_marker_preserves_average(self):
        window = self.window()
        window.start_sweep()
        window.finish_sweep()
        window.on_timer(None)
        window.set_status(scope.STATUS.replace('MF0093500000', 'MF0093502000'))
        self.assertEqual(len(window.spectrum.history), 1)
        window.finish_sweep()
        window.on_timer(None)
        window.set_status(scope.STATUS.replace('CF0093500000', 'CF0093600000'))
        self.assertEqual(len(window.spectrum.history), 0)
        window.finish_sweep()
        window.on_timer(None)
        window.set_status(scope.STATUS.replace('CF0093500000', 'CF0093600000').replace('SW7', 'SW6'))
        self.assertEqual(len(window.spectrum.history), 0)

    def test_invalid_sweep_does_not_contribute(self):
        window = self.window()
        window.start_sweep()
        with contextlib.redirect_stderr(io.StringIO()):
            window.add_ds_line('DS0031:' + 'A' * 32)
        self.assertEqual(len(window.spectrum.history), 0)

    def test_mode_edit_during_dump_keeps_original_average_processing(self):
        window = self.window()
        window.start_sweep()
        window.select_mode('Statistic', hardware=True)
        window.finish_sweep()
        self.assertEqual(window.statistic.sweeps, 0)
        self.assertEqual(len(window.spectrum.history), 1)
        window.on_timer(None)
        window.set_status(scope.STATUS)
        window.finish_sweep()
        self.assertEqual(window.statistic.sweeps, 0)
        self.assertEqual(len(window.spectrum.history), 2)
        self.assertEqual(window.run_config.mode, 'Average')


    def test_progress_text(self):
        window = self.window()
        self.assertEqual(window.message.label, 'Average: 0/10 sweeps')
        for _ in range(10):
            window.spectrum.add_sweep((5,) * 128, window.status)
        window.update_statistic_info()
        self.assertEqual(window.message.label, 'Average: 10-sweep rolling window')
        window.average_window.SetSelection(0)
        window.on_average_window(None)
        self.assertEqual(window.message.label, 'Average: latest sweep')


@unittest.skipUnless(GUI, 'Use --gui for the focused offline window check')
class GuiTests(unittest.TestCase):
    def test_spectrum_controls_and_both_scales_render_offline(self):
        import wx
        app = wx.GetApp() or wx.App(False)
        parent = wx.Frame(None)
        fake = scope.Window()
        for name, value in vars(fake.controller).items():
            setattr(parent, name, value)
        window = scope.main.BandscopeWindow(parent)
        try:
            window.Show()
            wx.Yield()
            self.assertFalse(window.average_window.IsEnabled())
            self.assertEqual(window.average_window.GetItems(), ['1', '2', '5', '10', '20', '50', '100'])
            self.assertEqual(window.average_window.GetStringSelection(), '10')
            self.assertEqual(window.spectrum_scale.GetStringSelection(), 'Relative %')
            window.status = STATUS.copy()
            window.trace_status = STATUS.copy()
            parent.connected = False
            window.mode_radio.SetSelection(4)
            window.on_sweep_mode(None)
            window.spectrum.add_sweep((0, 1, 7, 15) * 32, STATUS)
            before = deepcopy(vars(window.spectrum))
            for selection in (0, 1):
                window.spectrum_scale.SetSelection(selection)
                window.on_spectrum_scale(None)
                window.plot.Refresh()
                wx.Yield()
                self.assertEqual(vars(window.spectrum), before)
            self.assertFalse(hasattr(window, 'loop_checkbox'))
            right = window.clear_button.GetRect().GetRight()
            self.assertLessEqual(right, window.clear_button.GetParent().GetClientSize().width)
            self.assertEqual(fake.commands, [])
        finally:
            window.timer.Stop()
            window.Destroy()
            parent.Destroy()
            wx.Yield()


if __name__ == '__main__':
    unittest.main()
