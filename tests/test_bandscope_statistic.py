"""Focused Statistic tests; fake serial only, never connect to hardware."""
import contextlib
import io
import sys
from copy import deepcopy
from random import Random
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_current_bank_scan import main
from aor_functions import BandscopeStatistic, BandscopeAverage, BandscopeView, parse_bandscope_status

GUI = '--gui' in sys.argv
if GUI:
    sys.argv.remove('--gui')


STATUS = 'AM PH0 CF0093500000 MF0093500000 SW7'
RECEIVER = 'VF RF0093500000 ST100000 AU0 MD0 AT0'


class Control:
    def __init__(self, value=False):
        self.value = value
        self.enabled = True
        self.label = ''

    def IsChecked(self):
        return self.value

    GetValue = IsChecked
    GetSelection = IsChecked

    def SetValue(self, value):
        self.value = value

    SetSelection = SetValue

    def Enable(self, enabled):
        self.enabled = bool(enabled)

    def SetLabel(self, label):
        self.label = label


class Window:
    """Exercise actual window event/lifecycle methods without a display server."""
    def __init__(self):
        self.commands = []
        self.controller = SimpleNamespace(
            connected=True, serial=SimpleNamespace(is_open=True),
            alive=SimpleNamespace(is_set=lambda: True),
            memory_inventory=None, memory_bank_transfer=None,
            normal_vfo_command=lambda: b'VF\r\n', validate_fields=Mock(),
            write_serial=self.write)
        for name in ('start_button', 'stop_button', 'clear_button',
                     'tune_button', 'centre_button', 'message'):
            setattr(self, name, Control())
        self.mode_radio = Control(0)
        self.sweep_mode = 'Single'
        self.run_config = self.last_run_config = None
        self.SWEEP_MODES = main.BandscopeWindow.SWEEP_MODES
        self.threshold = Control(2)
        self.average_window, self.spectrum_scale = Control(3), Control(0)
        self.span_choice, self.centre_input, self.info, self.plot = (Mock() for _ in range(4))
        self.span_choice = Control(6)
        self.centre_input.GetValue.return_value = '93.500000'
        self.timer = Mock()
        self.status = parse_bandscope_status(STATUS)
        self.receiver_state = main.BandscopeReceiverState()
        self.receiver_state.confirm(self.status)
        self.local_geometry = None
        self.local_marker_hz = None
        self.pending_centre_hz = None
        self.requested_geometry = None
        self.geometry_reconciled = False
        self.samples = self.trace_status = self.sweep_status = self.sweep = None
        self.sweep_requested = self.running = self.stop_requested = False
        self.waiting = None
        self.entered = True
        self.restore_command = b'VF\r\n'
        self.close_requested = False
        self.started_at = self.last_duration = self.pending_tune = None
        self.setting_command = b''
        self.expected_entry_geometry = None
        self.peak_value = self.peak_pending = False
        self.statistic = BandscopeStatistic()
        self.statistic_generation = 0
        self.sweep_statistic_generation = None
        self.spectrum = BandscopeAverage()
        self.spectrum_generation = 0
        self.sweep_spectrum_generation = None
        self.peak_target = self.peak_continuation = None
        self.peak_restore_query = True
        self.view = BandscopeView()
        self.drag_start = self.drag_end = None
        self.drag_moved = False
        self.plot.GetClientSize.return_value = (800, 400)
        self.plot.HasCapture.return_value = False
        self.set_controls_enabled(True)

    def write(self, command):
        self.commands.append(command)
        return len(command)

    def select_mode(self, mode, hardware=False):
        self.mode_radio.SetSelection(self.SWEEP_MODES.index(mode))
        connected = self.controller.connected
        if not hardware:
            self.controller.connected = False
        self.on_sweep_mode(None)
        self.controller.connected = connected
        self.set_controls_enabled(self.waiting is None)

    def enable_statistic(self):
        self.select_mode('Statistic')

    def start_sweep(self, peak=False):
        self.on_start(None)
        if self.waiting == 'receiver':
            self.capture_receiver(RECEIVER)
        self.set_status(STATUS.replace('PH0', 'PH1' if peak else 'PH0'))

    def finish_sweep(self, level='5', count=128):
        for index in range(count - 1, 30, -32):
            self.add_ds_line('DS%04d:%s' % (index, level * 32))


for _name, _method in main.BandscopeWindow.__dict__.items():
    if not _name.startswith('__') and callable(_method):
        setattr(Window, _name, _method)


class StatisticTests(unittest.TestCase):
    def test_repeated_sweeps_cannot_exceed_100_for_any_threshold(self):
        random = Random(83)
        status = parse_bandscope_status(STATUS)
        for threshold in range(2, 16):
            accumulator = BandscopeStatistic(threshold)
            for _ in range(100):
                values = tuple(random.randrange(16) for _ in range(128))
                percentages = accumulator.add_sweep(values, status)
                self.assertTrue(all(a <= v for a, v in zip(accumulator.active, accumulator.valid)))
                self.assertTrue(all(value is None or 0 <= value <= 100 for value in percentages))

    def test_corrupt_counters_are_rejected_not_clamped(self):
        for active, valid in ((2, 1), (1, 0), (-1, 1), (1, -1), (0, 2)):
            accumulator = BandscopeStatistic()
            accumulator.active, accumulator.valid, accumulator.sweeps = [active], [valid], 1
            with self.assertRaisesRegex(ValueError, 'Invalid Statistic counters'):
                accumulator.percentages()
        accumulator.valid = []
        with self.assertRaisesRegex(ValueError, 'bin counts differ'):
            accumulator.percentages()

    def test_activity_and_invalid_denominators(self):
        accumulator = BandscopeStatistic(5)
        status = parse_bandscope_status(STATUS)
        accumulator.add_sweep((0, 1, 2, 5, 15) + (2,) * 123, status)
        result = accumulator.add_sweep((5, 1, 5, 2, 0) + (2,) * 123, status)
        self.assertEqual(result[:5], (100, None, 50, 50, 100))
        self.assertEqual(accumulator.valid[:5], [1, 0, 2, 2, 1])
        self.assertEqual(accumulator.active[:5], [1, 0, 1, 1, 1])
        self.assertEqual(accumulator.sweeps, 2)

    def test_invalid_sweeps_do_not_mutate(self):
        accumulator = BandscopeStatistic()
        status = parse_bandscope_status(STATUS)
        accumulator.add_sweep((5,) * 128, status)
        for values in ((5,) * 127, (5,) * 127 + (16,), (5,) * 127 + ('5',)):
            with self.assertRaises(ValueError):
                accumulator.add_sweep(values, status)
            self.assertEqual(accumulator.sweeps, 1)
            self.assertEqual(accumulator.valid, [1] * 128)

    def test_geometry_count_threshold_and_marker(self):
        accumulator = BandscopeStatistic()
        status = parse_bandscope_status(STATUS)
        accumulator.add_sweep((5,) * 128, status)
        status['marker_hz'] += 2000
        accumulator.add_sweep((5,) * 128, status)
        self.assertEqual(accumulator.sweeps, 2)
        for field, value in (('centre_hz', 94000000), ('span_code', 6)):
            status[field] = value
            accumulator.add_sweep((5,) * 128, status)
            self.assertEqual(accumulator.sweeps, 1)
        accumulator.add_sweep((5,) * 1024, status)
        self.assertEqual(accumulator.sweeps, 1)
        self.assertEqual(len(accumulator.valid), 1024)
        accumulator.set_threshold(6)
        self.assertEqual(accumulator.sweeps, 0)
        self.assertEqual(accumulator.active, [])


class StartBoundaryTests(unittest.TestCase):
    def test_set_centre_before_first_sweep_updates_only_local_geometry(self):
        window = Window()
        window.status = None
        window.entered = False
        window.centre_input.GetValue.return_value = '94.25'
        window.span_choice.SetSelection(5)
        window.on_centre(None)
        self.assertEqual(window.commands, [])
        self.assertEqual(window.local_geometry['centre_hz'], 94250000)
        self.assertEqual(window.local_geometry['span_hz'], 200000)
        self.assertEqual((window.view.frequency_low, window.view.frequency_high), (94150000, 94350000))
        self.assertIsNone(window.status)
        self.assertFalse(window.entered)
        self.assertEqual(window.span_choice.GetSelection(), 5)
        self.assertNotIn(b'DS\r\n', window.commands)
        self.assertTrue(all(not line.startswith(b'PH') for command in window.commands
                            for line in command.splitlines()))

    def test_every_passive_control_is_silent_and_values_survive_edits(self):
        window = Window()
        for mode in window.SWEEP_MODES:
            window.select_mode(mode, hardware=True)
            window.span_choice.SetSelection(5)
            window.on_span(None)
            window.threshold.SetValue(8)
            window.on_threshold(None)
            window.average_window.SetSelection(4)
            window.on_average_window(None)
            window.spectrum_scale.SetSelection(1)
            window.on_spectrum_scale(None)
            self.assertEqual(window.commands, [])
            self.assertEqual(window.span_choice.GetSelection(), 5)
            self.assertEqual(window.mode_radio.GetSelection(), window.SWEEP_MODES.index(mode))
            self.assertFalse(window.running)
            self.assertIsNone(window.run_config)

    def test_start_reads_widgets_without_needing_change_events_and_freezes_run(self):
        window = Window()
        window.mode_radio.SetSelection(2)  # Peak Hold; no change event delivered.
        window.span_choice.SetSelection(5)
        window.centre_input.GetValue.return_value = '94.25'
        window.threshold.SetValue(9)
        window.average_window.SetSelection(4)
        window.spectrum_scale.SetSelection(1)
        window.on_start(None)
        config = window.run_config
        self.assertEqual((config.mode, config.span_code, config.centre_hz, config.threshold,
                          config.average_window, config.average_scale),
                         ('Peak Hold', 6, 94250000, 9, 20, 'Raw'))
        with self.assertRaises(FrozenInstanceError):
            config.mode = 'Single'
        self.assertEqual(window.commands, [b'CF0094250000\r\nSW6\r\nAM\r\n'])
        window.select_mode('Single', hardware=True)
        window.span_choice.SetSelection(0)
        window.on_span(None)
        window.centre_input.GetValue.return_value = '100.5'
        window.threshold.SetValue(2)
        window.on_threshold(None)
        window.average_window.SetSelection(0)
        window.on_average_window(None)
        window.spectrum_scale.SetSelection(0)
        window.on_spectrum_scale(None)
        self.assertIs(window.run_config, config)
        self.assertEqual(len(window.commands), 1)
        status = 'AM PH0 CF0094250000 MF0093500000 SW6'
        window.set_status(status)
        self.assertEqual(window.commands[-1], b'PH1\r\nPH\r\n')
        window.set_peak_hold('PH1')
        window.finish_sweep('F')
        self.assertTrue(window.running)
        window.on_timer(None)
        window.set_status(status.replace('PH0', 'PH1'))
        self.assertEqual(window.commands[-1], b'DS\r\n')
        self.assertIs(window.run_config, config)
        self.assertEqual(window.span_choice.GetSelection(), 0)
        self.assertEqual(window.centre_input.GetValue(), '100.5')

    def test_scale_edit_does_not_change_current_run_or_drop_history(self):
        window = Window()
        window.select_mode('Average')
        window.spectrum_scale.SetSelection(1)
        window.start_sweep()
        window.finish_sweep('7')
        before = list(window.commands)
        window.spectrum_scale.SetSelection(0)
        window.on_spectrum_scale(None)
        self.assertFalse(window.percentage_scale())
        self.assertEqual(window.commands, before)
        self.assertEqual(len(window.spectrum.history), 1)
        window.on_stop(None)
        window.capture_receiver(RECEIVER)
        window.start_sweep()
        self.assertTrue(window.percentage_scale())
        self.assertEqual(len(window.spectrum.history), 1)

    def test_initial_unknown_centre_is_resolved_from_hardware_without_rereading_ui(self):
        window = Window()
        window.entered = False
        window.status = None
        window.span_choice.SetSelection(0)
        window.centre_input.GetValue.return_value = ''
        window.on_start(None)
        self.assertIsNone(window.run_config.centre_hz)
        window.capture_receiver(RECEIVER)
        self.assertEqual(window.commands[-1], b'AM\r\nSW1\r\nAM\r\n')
        window.centre_input.GetValue.return_value = '100.5'  # Pending next-Start edit.
        window.set_status('AM PH0 CF0093500000 MF0093500000 SW1')
        self.assertEqual(window.run_config.centre_hz, 93500000)
        self.assertEqual(window.run_config.marker_hz, 93500000)
        self.assertEqual(window.commands[-1], b'DS\r\n')

    def test_invalid_start_configuration_sends_nothing(self):
        window = Window()
        window.centre_input.GetValue.return_value = 'bad'
        window.on_start(None)
        self.assertFalse(window.running)
        self.assertIsNone(window.run_config)
        self.assertEqual(window.commands, [])


class GeometryTests(unittest.TestCase):
    STATUS = 'AM PH0 CF0093500000 MF0093512000 SW6'

    def window(self):
        window = Window()
        window.status = parse_bandscope_status(self.STATUS)
        window.receiver_state.confirm(window.status)
        window.span_choice = Control(5)
        window.view.reset(window.status, False)
        window.view.zoom((200, 100), (600, 250), window.plot_rect())
        return window

    def assert_geometry(self, window, x_bounds):
        self.assertEqual(tuple(window.status[key] for key in ('centre_hz', 'span_code', 'marker_hz')),
                         (93500000, 6, 93512000))
        self.assertEqual(window.status['span_hz'], 200000)
        self.assertEqual(window.span_choice.GetSelection(), 5)
        self.assertEqual((window.view.frequency_low, window.view.frequency_high), x_bounds)

    def test_every_stopped_transition_is_passive_and_preserves_geometry(self):
        for entered in (False, True):
            for source in Window().SWEEP_MODES:
                for target in Window().SWEEP_MODES:
                    window = self.window()
                    window.select_mode(source)
                    window.peak_value = source == 'Peak Hold'
                    window.entered = entered
                    x = window.view.frequency_low, window.view.frequency_high
                    window.select_mode(target, hardware=True)
                    self.assertEqual(window.commands, [])
                    self.assert_geometry(window, x)
                    self.assertEqual(window.entered, entered)
                    self.assertFalse(window.running)
                    self.assertIsNone(window.run_config)


    def test_running_mode_edits_preserve_original_run_and_geometry(self):
        for source in Window().SWEEP_MODES:
            for target in Window().SWEEP_MODES:
                if source == target:
                    continue
                window = self.window()
                window.select_mode(source)
                status = self.STATUS.replace('PH0', 'PH%d' % (source == 'Peak Hold'))
                window.on_start(None)
                window.set_status(status)
                config = window.run_config
                x = window.view.frequency_low, window.view.frequency_high
                before = list(window.commands)
                window.select_mode(target, hardware=True)
                self.assertEqual(window.commands, before)
                self.assertIs(window.run_config, config)
                self.assertEqual(window.processing_mode(), source)
                window.finish_sweep()
                if source == 'Single':
                    self.assertEqual(window.waiting, 'restore')
                else:
                    window.on_timer(None)
                    window.set_status(status)
                    self.assertEqual(window.waiting, 'data')
                    self.assertEqual(window.commands[len(before):], [b'AM\r\n', b'DS\r\n'])
                self.assert_geometry(window, x)


    def test_real_reentry_restores_cache_and_rejects_early_default_status(self):
        window = self.window()
        window.entered = False
        x = window.view.frequency_low, window.view.frequency_high
        window.on_start(None)
        self.assertEqual(window.commands, [b'RX\r\n'])
        window.capture_receiver(RECEIVER)
        self.assertEqual(window.commands[-1], b'AM\r\nAM\r\n')
        window.set_status('AM PH1 CF0091000000 MF0091000000 SW1')
        self.assertEqual(window.commands[-1], b'CF0093500000\r\nSW6\r\nAM\r\n')
        self.assert_geometry(window, x)
        self.assertEqual(window.waiting, 'status')
        self.assertNotIn(b'DS\r\n', window.commands)
        window.set_status(self.STATUS)
        self.assertEqual(window.commands[-1], b'DS\r\n')
        self.assert_geometry(window, x)

    def test_rx_already_in_analyser_does_not_accept_unrestored_geometry(self):
        window = self.window()
        window.entered = False
        x = window.view.frequency_low, window.view.frequency_high
        window.on_start(None)
        window.set_status('AM PH0 CF0091000000 MF0091000000 SW1')
        self.assertEqual(window.commands[-1], b'CF0093500000\r\nSW6\r\nAM\r\n')
        self.assert_geometry(window, x)
        self.assertNotIn(b'DS\r\n', window.commands)
        window.set_status(self.STATUS)
        self.assertEqual(window.commands[-1], b'DS\r\n')


class ModeTests(unittest.TestCase):
    def test_all_five_acquisition_modes_and_ph_gate(self):
        for mode in Window().SWEEP_MODES:
            with self.subTest(mode=mode):
                window = Window()
                window.select_mode(mode)
                target = mode == 'Peak Hold'
                window.start_sweep(peak=not target)
                self.assertEqual(window.commands[-1], ('PH%d\r\nPH\r\n' % target).encode('ascii'))
                self.assertNotIn(b'DS\r\n', window.commands)
                window.set_peak_hold('PH%d' % target)
                self.assertEqual(window.commands.count(b'DS\r\n'), 1)
                window.finish_sweep('F')
                if mode == 'Single':
                    self.assertFalse(window.running)
                    self.assertEqual(window.waiting, 'restore')
                else:
                    self.assertTrue(window.running)
                    self.assertIsNone(window.waiting)
                    window.timer.StartOnce.assert_called_with(500)
                    window.on_timer(None)
                    window.set_status(STATUS.replace('PH0', 'PH%d' % target))
                    self.assertEqual(window.commands.count(b'DS\r\n'), 2)
                    window.on_stop(None)
                    window.finish_sweep('F')
                self.assertEqual(window.commands[-1], b'VF\r\nRX\r\n')

    def test_queued_peak_hold_requires_next_explicit_start(self):
        window = Window()
        window.select_mode('Loop')
        window.start_sweep()
        window.finish_sweep()
        before = list(window.commands)
        window.select_mode('Peak Hold', hardware=True)
        self.assertEqual(window.commands, before)
        window.on_timer(None)
        window.set_status(STATUS)
        self.assertEqual(window.commands[-1], b'DS\r\n')
        self.assertEqual(window.run_config.mode, 'Loop')
        self.assertNotIn(b'PH1\r\nPH\r\n', window.commands)
        window.on_stop(None)
        window.finish_sweep()
        window.capture_receiver(RECEIVER)
        window.start_sweep()
        self.assertEqual(window.commands[-1], b'PH1\r\nPH\r\n')
        window.set_peak_hold('PH1')
        self.assertEqual(window.commands[-1], b'DS\r\n')


    def test_single_mode_and_enable_matrix(self):
        window = Window()
        self.assertEqual(window.sweep_mode, 'Single')
        for mode in window.SWEEP_MODES:
            window.select_mode(mode)
            self.assertEqual(window.sweep_mode, mode)
            self.assertEqual(window.mode_radio.GetSelection(), window.SWEEP_MODES.index(mode))
            self.assertFalse(hasattr(window, 'loop_checkbox'))
            self.assertEqual(window.threshold.enabled, mode == 'Statistic')
            self.assertEqual(window.average_window.enabled, mode == 'Average')
            self.assertEqual(window.spectrum_scale.enabled, mode == 'Average')
            self.assertEqual(window.clear_button.enabled, mode in ('Statistic', 'Average'))
            self.assertFalse(hasattr(window, 'statistic_checkbox'))
            self.assertFalse(hasattr(window, 'spectrum_checkbox'))

    def test_common_clear_only_resets_current_accumulator(self):
        window = Window()
        for mode in window.SWEEP_MODES:
            window.select_mode(mode)
            window.statistic.add_sweep((15,) * 128, window.status)
            window.spectrum.add_sweep((15,) * 128, window.status)
            before = deepcopy((window.status, vars(window.view), window.running, window.waiting))
            window.on_clear(None)
            self.assertEqual(window.statistic.sweeps, 0 if mode == 'Statistic' else 1)
            self.assertEqual(len(window.spectrum.history), 0 if mode == 'Average' else 1)
            self.assertEqual((window.status, vars(window.view), window.running, window.waiting), before)
            self.assertEqual(window.commands, [])
            window.statistic.clear()
            window.spectrum.clear()

    def test_all_mode_choices_are_passive(self):
        window = Window()
        for mode in window.SWEEP_MODES:
            window.select_mode(mode, hardware=True)
            self.assertEqual(window.commands, [])
            self.assertIsNone(window.waiting)
            self.assertIsNone(window.run_config)
            self.assertFalse(window.running)


    def test_mode_change_preserves_x_zoom_and_resets_y_only(self):
        window = Window()
        window.view.reset(window.status, False)
        window.view.zoom((200, 100), (600, 250), window.plot_rect())
        x = window.view.frequency_low, window.view.frequency_high
        for mode in ('Statistic', 'Average', 'Peak Hold', 'Single'):
            window.select_mode(mode)
            self.assertEqual((window.view.frequency_low, window.view.frequency_high), x)
            self.assertEqual((window.view.y_low, window.view.y_high),
                             (0, 100 if mode in ('Statistic', 'Average') else 15))

    def test_queued_statistic_does_not_change_active_peak_hold(self):
        window = Window()
        window.select_mode('Peak Hold')
        window.start_sweep(peak=True)
        window.select_mode('Statistic', hardware=True)
        window.finish_sweep('F')
        self.assertEqual(window.statistic.sweeps, 0)
        window.on_timer(None)
        window.set_status(STATUS.replace('PH0', 'PH1'))
        self.assertEqual(window.commands[-1], b'DS\r\n')
        self.assertEqual(window.processing_mode(), 'Peak Hold')
        self.assertNotIn(b'PH0\r\nPH\r\n', window.commands)
        window.on_stop(None)
        window.finish_sweep()
        window.capture_receiver(RECEIVER)
        window.start_sweep(peak=True)
        self.assertEqual(window.commands[-1], b'PH0\r\nPH\r\n')
        window.set_peak_hold('PH0')
        window.finish_sweep('F')
        self.assertEqual(window.statistic.sweeps, 1)



class LifecycleTests(unittest.TestCase):
    def test_continuous_without_loop_and_retains_after_stop_restart(self):
        window = Window()
        window.enable_statistic()
        self.assertFalse(hasattr(window, 'loop_checkbox'))
        self.assertTrue(window.threshold.enabled)
        window.start_sweep()
        window.finish_sweep()
        self.assertEqual(window.statistic.sweeps, 1)
        window.timer.StartOnce.assert_called_with(500)
        window.on_timer(None)
        window.set_status(STATUS)
        window.on_stop(None)
        window.finish_sweep()
        self.assertEqual(window.statistic.sweeps, 2)
        self.assertEqual(window.waiting, 'restore')
        self.assertEqual(window.commands[-1], b'VF\r\nRX\r\n')
        window.capture_receiver(RECEIVER)
        window.start_sweep()
        window.finish_sweep()
        self.assertEqual(window.statistic.sweeps, 3)

    def test_peak_hold_off_verified_before_statistic_and_stays_off_after_stop(self):
        window = Window()
        window.enable_statistic()
        window.start_sweep(peak=True)
        self.assertEqual(window.commands[-1], b'PH0\r\nPH\r\n')
        self.assertNotIn(b'DS\r\n', window.commands)
        window.set_peak_hold('PH0')
        self.assertEqual(window.commands[-1], b'DS\r\n')
        window.on_stop(None)
        window.finish_sweep()
        self.assertEqual(window.commands[-1], b'VF\r\nRX\r\n')
        self.assertFalse(window.peak_value)
        self.assertEqual(window.statistic.sweeps, 1)

    def test_switching_ui_to_single_does_not_stop_running_statistic(self):
        window = Window()
        window.enable_statistic()
        window.start_sweep()
        window.select_mode('Single', hardware=True)
        window.finish_sweep()
        self.assertTrue(window.running)
        self.assertEqual(window.processing_mode(), 'Statistic')
        window.on_stop(None)
        window.capture_receiver(RECEIVER)
        window.start_sweep()
        window.finish_sweep()
        self.assertEqual(window.commands[-1], b'VF\r\nRX\r\n')


    def test_disconnect_keeps_mode_ph_state_and_restores_receiver(self):
        window = Window()
        window.enable_statistic()
        window.start_sweep(peak=True)
        window.set_peak_hold('PH0')
        window.disconnect()
        self.assertEqual(window.commands[-1], b'VF\r\n')
        self.assertFalse(window.peak_value)

    def test_failed_dump_never_counts(self):
        for bad_lines in (['DS0031:' + '5' * 32],
                          ['DS0127:' + '5' * 32, 'DS0127:' + '6' * 32,
                           'DS0095:' + '5' * 32, 'DS0063:' + '5' * 32, 'DS0031:' + '5' * 32]):
            window = Window()
            window.enable_statistic()
            window.start_sweep()
            with contextlib.redirect_stderr(io.StringIO()):
                for line in bad_lines:
                    window.add_ds_line(line)
            self.assertEqual(window.statistic.sweeps, 0)
            self.assertFalse(window.running)

    def test_clear_and_threshold_do_not_send_commands_or_stop(self):
        window = Window()
        window.enable_statistic()
        window.start_sweep()
        window.finish_sweep()
        before = list(window.commands)
        window.on_clear(None)
        self.assertEqual(window.statistic.sweeps, 0)
        self.assertTrue(window.running)
        self.assertEqual(window.commands, before)
        window.threshold.SetValue(8)
        window.on_threshold(None)
        self.assertEqual(window.statistic.threshold, 2)
        self.assertEqual(window.threshold.GetValue(), 8)
        self.assertEqual(window.commands, before)

    def test_threshold_edit_during_dump_is_for_next_start_only(self):
        window = Window()
        window.enable_statistic()
        window.start_sweep()
        window.threshold.SetValue(8)
        before = list(window.commands)
        window.on_threshold(None)
        self.assertEqual(window.commands, before)
        window.finish_sweep('5')
        self.assertEqual(window.statistic.sweeps, 1)
        self.assertEqual(window.statistic.threshold, 2)
        self.assertEqual(window.statistic.percentages(), (100,) * 128)
        window.on_stop(None)
        window.capture_receiver(RECEIVER)
        window.start_sweep()
        window.finish_sweep('5')
        self.assertEqual(window.statistic.threshold, 8)
        self.assertEqual(window.statistic.percentages(), (0,) * 128)


    def test_geometry_resets_but_marker_does_not(self):
        window = Window()
        window.enable_statistic()
        window.start_sweep()
        window.finish_sweep()
        window.on_timer(None)
        window.set_status(STATUS.replace('MF0093500000', 'MF0093502000'))
        self.assertEqual(window.statistic.sweeps, 1)
        window.finish_sweep()
        window.on_timer(None)
        window.set_status(STATUS.replace('CF0093500000', 'CF0093600000'))
        self.assertEqual(window.statistic.sweeps, 0)

    def test_normal_mode_remains_single_sweep(self):
        window = Window()
        window.start_sweep()
        self.assertEqual(window.commands[-1], b'DS\r\n')
        window.finish_sweep()
        self.assertEqual(window.commands[-1], b'VF\r\nRX\r\n')
        self.assertEqual(window.statistic.sweeps, 0)
        self.assertFalse(window.peak_value)

    def test_peak_disable_failure_does_not_collect_or_retry_ds(self):
        window = Window()
        window.enable_statistic()
        window.start_sweep(peak=True)
        with contextlib.redirect_stderr(io.StringIO()):
            window.set_peak_hold('PH1')
        self.assertNotIn(b'DS\r\n', window.commands)
        self.assertFalse(window.running)
        self.assertEqual(window.commands[-1], b'VF\r\nRX\r\n')

    def test_close_drains_then_restores_and_retains_results(self):
        window = Window()
        window.Hide = Mock()
        window.enable_statistic()
        window.start_sweep(peak=True)
        window.set_peak_hold('PH0')
        window.on_close(None)
        self.assertEqual(window.waiting, 'data')
        window.finish_sweep()
        self.assertEqual(window.statistic.sweeps, 1)
        self.assertEqual(window.commands[-1], b'VF\r\nRX\r\n')
        self.assertFalse(window.running)


class ZoomTests(unittest.TestCase):
    @staticmethod
    def event(x, y):
        return SimpleNamespace(GetPosition=lambda: (x, y))

    def test_percentage_transform_top_and_bottom_and_zoom(self):
        view = BandscopeView()
        view.reset(parse_bandscope_status(STATUS), True)
        rect = (55, 20, 780, 355)
        self.assertEqual(view.y_at(100, rect), 20)
        self.assertEqual(view.y_at(0, rect), 355)
        self.assertEqual(view.y_at(50, rect), 187)
        view.y_low, view.y_high = 80, 100
        self.assertEqual(view.y_at(100, rect), 20)
        self.assertEqual(view.y_at(80, rect), 355)

    def test_drag_display_only_and_statistics_survive(self):
        for statistic in (False, True):
            window = Window()
            if statistic:
                window.enable_statistic()
                window.statistic.add_sweep((15,) * 128, window.status)
            window.view.sync(window.status, statistic)
            before = deepcopy((window.status, window.trace_status, window.samples, vars(window.statistic)))
            window.on_plot_left_down(self.event(200, 100))
            window.on_plot_motion(self.event(600, 250))
            self.assertTrue(window.drag_moved)
            window.on_plot_left_up(self.event(600, 250))
            self.assertGreater(window.view.frequency_low, 93450000)
            self.assertLess(window.view.frequency_high, 93550000)
            self.assertGreater(window.view.y_low, 0)
            self.assertLess(window.view.y_high, 100 if statistic else 15)
            self.assertEqual(window.commands, [])
            self.assertEqual((window.status, window.trace_status, window.samples, vars(window.statistic)), before)
            window.on_reset_zoom(None)
            self.assertEqual((window.view.frequency_low, window.view.frequency_high), (93450000, 93550000))
            self.assertEqual((window.view.y_low, window.view.y_high), (0, 100 if statistic else 15))
            self.assertEqual(window.commands, [])

    def test_double_click_moves_local_marker_using_zoomed_frequency_mapping(self):
        window = Window()
        window.view.reset(window.status, False)
        window.view.frequency_low, window.view.frequency_high = 93500000, 93520000
        window.on_plot_left_down(self.event(417, 150))
        window.on_plot_left_up(self.event(417, 150))
        window.on_plot_double_click(self.event(417, 150))
        self.assertEqual(window.commands, [])
        self.assertEqual(window.local_marker_hz, 93510000)
        self.assertEqual(window.statistic.sweeps, 0)

    def test_zoom_works_during_ds_acquisition_without_sending_commands(self):
        window = Window()
        window.enable_statistic()
        window.start_sweep()
        before = list(window.commands)
        window.on_plot_left_down(self.event(200, 100))
        window.on_plot_left_up(self.event(600, 250))
        self.assertEqual(window.commands, before)
        self.assertEqual(window.waiting, 'data')
        window.finish_sweep('F')
        self.assertEqual(window.statistic.sweeps, 1)
        self.assertGreater(window.view.y_low, 0)

    def test_geometry_resets_zoom_marker_does_not_threshold_resets_y(self):
        window = Window()
        window.enable_statistic()
        window.view.zoom((200, 100), (600, 250), window.plot_rect())
        before = vars(window.view).copy()
        status = window.status.copy()
        status['marker_hz'] += 2000
        window.view.sync(status, True)
        self.assertEqual(vars(window.view), before)
        window.threshold.SetValue(10)
        window.on_threshold(None)
        self.assertEqual((window.view.y_low, window.view.y_high), (0, 100))
        self.assertEqual(window.view.frequency_low, before['frequency_low'])
        for field, value in (('centre_hz', 94000000), ('span_hz', 200000)):
            window.view.zoom((200, 100), (600, 250), window.plot_rect())
            status[field] = value
            window.view.sync(status, True)
            self.assertEqual(window.view.frequency_low, status['centre_hz'] - status['span_hz'] / 2)
            self.assertEqual((window.view.y_low, window.view.y_high), (0, 100))

    def test_small_or_one_axis_drag_does_not_move_marker(self):
        window = Window()
        window.on_plot_left_down(self.event(200, 100))
        window.on_plot_left_up(self.event(600, 100))
        self.assertEqual(window.commands, [])

    def test_trace_installs_graph_clip_and_restricts_x(self):
        window = Window()
        window.trace_status = window.status.copy()
        window.view.reset(window.status, True)
        window.view.frequency_low, window.view.frequency_high = 93500000, 93504000
        window.view.y_low, window.view.y_high = 80, 100
        dc = Mock()
        clip = Mock(__enter__=Mock(), __exit__=Mock(return_value=False))
        with patch.object(main.wx, 'DCClipper', return_value=clip) as clipping, \
                patch.object(main.wx, 'Pen'):
            window.draw_trace(dc, window.plot_rect(), (0,) * 128, True)
        clipping.assert_called_once()
        self.assertEqual(tuple(clipping.call_args.args[1]), (55, 20, 726, 336))
        self.assertEqual(dc.DrawPoint.call_count, 1)
        self.assertEqual(dc.DrawLine.call_count, 2)
        clip.__enter__.assert_called_once()
        clip.__exit__.assert_called_once()

    def test_marker_outside_view_is_not_drawn(self):
        window = Window()
        window.view.reset(window.status, False)
        dc = Mock()
        dc.GetTextExtent.return_value = (100, 14)
        with patch.object(main.wx, 'AutoBufferedPaintDC', return_value=dc), \
                patch.object(main.wx, 'Pen'), patch.object(main.wx, 'Brush'):
            window.on_paint(None)
            labels = [call.args[0] for call in dc.DrawText.call_args_list]
            self.assertIn('93.500 MHz', labels)
            dc.reset_mock()
            window.view.frequency_low, window.view.frequency_high = 93510000, 93520000
            window.on_paint(None)
            labels = [call.args[0] for call in dc.DrawText.call_args_list]
            self.assertNotIn('93.500 MHz', labels)


@unittest.skipUnless(GUI, 'Use --gui for the focused offline window check')
class GuiTests(unittest.TestCase):
    def test_native_span_choice_survives_passive_mode_changes(self):
        import wx
        app = wx.GetApp() or wx.App(False)
        parent = wx.Frame(None)
        fake = Window()
        for name, value in vars(fake.controller).items():
            setattr(parent, name, value)
        window = main.BandscopeWindow(parent)
        try:
            window.status = parse_bandscope_status(GeometryTests.STATUS)
            window.span_choice.SetSelection(5)
            window.centre_input.ChangeValue('93.500000')
            window.view.reset(window.status, False)
            window.Show()
            for index, mode in enumerate(window.SWEEP_MODES):
                window.mode_radio.SetSelection(index)
                window.on_sweep_mode(None)
                if window.waiting == 'mode_peak':
                    window.set_peak_hold('PH%d' % (mode == 'Peak Hold'))
                wx.Yield()
                self.assertEqual(window.span_choice.GetStringSelection(), '200 kHz')
                self.assertEqual(window.centre_input.GetValue(), '93.500000')
                self.assertEqual(window.status['marker_hz'], 93512000)
                self.assertFalse(window.entered)
            self.assertEqual(fake.commands, [])
        finally:
            window.timer.Stop()
            window.Destroy()
            parent.Destroy()
            wx.Yield()

    def test_real_pixels_at_100_and_clipped_in_raw_and_statistic_zoom(self):
        import wx
        app = wx.GetApp() or wx.App(False)
        window = Window()
        window.trace_status = window.status.copy()
        rect = (55, 20, 220, 155)
        for statistic, samples in ((True, (100,) * 128), (True, (0, 100) * 64),
                                   (False, (2, 15) * 64)):
            window.view.reset(window.status, statistic)
            if statistic and samples[0] == 0:
                window.view.y_low, window.view.y_high = 80, 100
            elif not statistic:
                window.view.y_low, window.view.y_high = 5, 10
            bitmap = wx.Bitmap(240, 180)
            dc = wx.MemoryDC(bitmap)
            dc.SetBackground(wx.Brush('white'))
            dc.Clear()
            window.draw_trace(dc, rect, samples, statistic)
            dc.SelectObject(wx.NullBitmap)
            image = bitmap.ConvertToImage()
            colored = [(x, y) for y in range(180) for x in range(240)
                       if (image.GetRed(x, y), image.GetGreen(x, y), image.GetBlue(x, y)) != (255, 255, 255)]
            self.assertTrue(colored)
            self.assertTrue(all(55 <= x <= 220 and 20 <= y <= 155 for x, y in colored))
            if statistic and samples[0] == 100:
                self.assertEqual({y for x, y in colored}, {20})

    def test_controls_and_percentage_paint_without_hardware(self):
        import wx
        app = wx.GetApp() or wx.App(False)
        parent = wx.Frame(None)
        fake = Window()
        for name, value in vars(fake.controller).items():
            setattr(parent, name, value)
        window = main.BandscopeWindow(parent)
        try:
            window.Show()
            wx.Yield()
            self.assertEqual(window.mode_radio.GetCount(), 5)
            self.assertEqual([window.mode_radio.GetString(index) for index in range(5)],
                             list(window.SWEEP_MODES))
            self.assertEqual(window.mode_radio.GetSelection(), 0)
            self.assertTrue(all(window.mode_radio.IsItemShown(index) for index in range(5)))
            self.assertEqual(window.clear_button.GetLabel(), 'Clear')
            self.assertFalse(window.clear_button.IsEnabled())
            self.assertFalse(window.threshold.IsEnabled())
            parent.connected = False
            window.mode_radio.SetSelection(3)
            window.on_sweep_mode(None)
            self.assertFalse(hasattr(window, 'loop_checkbox'))
            self.assertTrue(window.threshold.IsEnabled())
            self.assertEqual((window.threshold.GetMin(), window.threshold.GetMax()), (2, 15))
            window.status = parse_bandscope_status(STATUS)
            window.trace_status = window.status.copy()
            window.statistic.add_sweep((0, 1, 2, 5) * 32, window.status)
            window.update_statistic_info()
            window.plot.Refresh()
            wx.Yield()  # Exercise the real wx percentage axis and sparse trace drawing.
            self.assertIn('1 sweeps', window.message.GetLabel())
            span_right = window.span_choice.GetRect().GetRight()
            self.assertLessEqual(span_right, window.span_choice.GetParent().GetClientSize().width)
            window.on_clear(None)
            wx.Yield()
            self.assertEqual(window.statistic.sweeps, 0)
            window.threshold.SetValue(7)
            # SetValue is deliberately silent; simulate the native user-edit event.
            event = wx.CommandEvent(wx.EVT_TEXT.typeId, window.threshold.GetId())
            event.SetEventObject(window.threshold)
            window.threshold.ProcessWindowEvent(event)
            wx.Yield()
            self.assertEqual(window.statistic.threshold, 7)
            window.span_choice.SetSelection(4)
            window.centre_input.ChangeValue('27.2')
            window.on_centre(None)
            window.set_controls_enabled(True)
            wx.Yield()  # Paint the local preview while disconnected.
            self.assertTrue(window.centre_button.IsEnabled())
            self.assertEqual((window.view.frequency_low, window.view.frequency_high), (26950000, 27450000))
            self.assertIsNone(window.trace_status)
            self.assertEqual(window.status['centre_hz'], 93500000)
            self.assertFalse(window.entered)
            window.on_marker_click = Mock(wraps=window.on_marker_click)
            for event_type in (wx.wxEVT_LEFT_DOWN, wx.wxEVT_LEFT_UP,
                               wx.wxEVT_LEFT_DCLICK, wx.wxEVT_LEFT_UP):
                mouse = wx.MouseEvent(event_type)
                mouse.SetPosition(wx.Point(400, 100))
                mouse.SetEventObject(window.plot)
                window.plot.ProcessWindowEvent(mouse)
            wx.Yield()
            window.on_marker_click.assert_called_once()
            self.assertIsNone(window.drag_start)
            self.assertFalse(window.plot.HasCapture())
            self.assertTrue(26950000 <= window.local_marker_hz <= 27450000)
            self.assertEqual(fake.commands, [])
        finally:
            window.timer.Stop()
            window.Destroy()
            parent.Destroy()
            wx.Yield()


if __name__ == '__main__':
    unittest.main()
