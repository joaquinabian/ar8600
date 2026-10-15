"""Differential receiver settings; actual window methods with fake serial only."""
import contextlib
import io
import unittest
from dataclasses import replace
from unittest.mock import Mock

from test_bandscope_statistic import Window, STATUS, RECEIVER
from aor_functions import BandscopeReceiverState, parse_bandscope_status
from command_console import send_command


def lines(window):
    return [line for data in window.commands for line in data.splitlines()]


class ReceiverStateTests(unittest.TestCase):
    def finish_single(self, window):
        window.finish_sweep()
        self.assertEqual(window.waiting, 'restore')
        window.capture_receiver(RECEIVER)
        self.assertFalse(window.entered)
        self.assertIsNone(window.run_config)

    def test_start_stop_start_unchanged_has_no_geometry_or_ph_commands(self):
        window = Window()
        window.start_sweep()
        self.finish_single(window)
        confirmed = dict(window.receiver_state.known)
        window.commands.clear()
        window.start_sweep()
        self.assertEqual(window.receiver_state.known, confirmed)
        self.assertEqual(window.commands, [b'RX\r\n', b'AM\r\nAM\r\n', b'DS\r\n'])

    def test_only_span_changes_and_reverting_is_not_dirty(self):
        window = Window()
        window.span_choice.SetSelection(4)
        window.on_span(None)
        window.on_start(None)
        self.assertEqual(window.commands, [b'SW5\r\nAM\r\n'])
        self.assertNotIn('span_code', window.receiver_state.known)  # Not confirmed by write.
        window.set_status(STATUS.replace('SW7', 'SW5'))
        self.assertEqual(window.receiver_state.known['span_code'], 5)

        window = Window()
        window.span_choice.SetSelection(4)
        window.on_span(None)
        window.span_choice.SetSelection(6)
        window.on_span(None)
        window.start_sweep()
        self.assertEqual(window.commands, [b'AM\r\n', b'DS\r\n'])

    def test_local_set_centre_is_applied_once_at_start_then_cached(self):
        window = Window()
        window.centre_input.GetValue.return_value = '94.25'
        window.on_centre(None)
        self.assertEqual(window.commands, [])
        self.assertEqual(window.receiver_state.known['centre_hz'], 93500000)
        window.on_start(None)
        self.assertEqual(window.commands, [b'CF0094250000\r\nAM\r\n'])
        self.assertNotIn('centre_hz', window.receiver_state.known)
        status = STATUS.replace('CF0093500000', 'CF0094250000')
        window.set_status(status)
        self.finish_single(window)
        self.assertEqual(window.receiver_state.known['centre_hz'], 94250000)
        window.commands.clear()
        window.on_start(None)
        window.capture_receiver(RECEIVER)
        window.set_status(status)
        self.assertEqual(window.commands, [b'RX\r\n', b'AM\r\nAM\r\n', b'DS\r\n'])

    def test_peak_readback_updates_cache_and_only_required_transition_is_sent(self):
        window = Window()
        window.select_mode('Peak Hold')
        window.start_sweep()
        self.assertEqual(window.commands[-1], b'PH1\r\nPH\r\n')
        self.assertNotIn('peak_hold', window.receiver_state.known)
        window.set_peak_hold('PH1')
        window.on_stop(None)
        window.finish_sweep()
        window.capture_receiver(RECEIVER)
        window.set_peak_hold('PH0')  # Confirmed restoration/external read-back.
        self.assertFalse(window.receiver_state.known['peak_hold'])
        window.commands.clear()
        window.select_mode('Single')
        window.start_sweep()
        self.assertFalse(any(line.startswith(b'PH') for line in lines(window)))
        self.finish_single(window)
        window.commands.clear()
        window.select_mode('Peak Hold')
        window.start_sweep()
        self.assertIn(b'PH1', lines(window))

    def test_invalidated_state_is_resynchronized_and_failed_write_stays_unknown(self):
        window = Window()
        window.receiver_state.invalidate('span_code')
        window.on_start(None)
        self.assertEqual(window.commands, [b'SW7\r\nAM\r\n'])
        self.assertNotIn('span_code', window.receiver_state.known)
        window.set_status(STATUS)
        self.assertEqual(window.receiver_state.known['span_code'], 7)

        window = Window()
        window.span_choice.SetSelection(4)
        window.controller.write_serial = Mock(return_value=0)
        window.on_start(None)
        self.assertNotIn('span_code', window.receiver_state.known)
        self.assertFalse(window.running)

    def test_unverified_write_timeout_invalidates_cache(self):
        window = Window()
        window.span_choice.SetSelection(4)
        window.on_start(None)
        with contextlib.redirect_stderr(io.StringIO()):
            window.on_timer(None)
        self.assertNotIn('span_code', window.receiver_state.known)

    def test_actual_am_reset_restores_only_observed_differences(self):
        window = Window()
        window.entered = False
        window.on_start(None)
        window.capture_receiver(RECEIVER)
        self.assertEqual(window.commands[-1], b'AM\r\nAM\r\n')
        window.set_status(STATUS.replace('SW7', 'SW1'))
        self.assertEqual(window.commands[-1], b'SW7\r\nAM\r\n')
        self.assertNotIn(b'DS\r\n', window.commands)
        window.set_status(STATUS)
        self.assertEqual(window.commands[-1], b'DS\r\n')

    def test_console_queries_preserve_cache_raw_actions_invalidate(self):
        window = Window()
        window.controller.bandscope = window
        known = dict(window.receiver_state.known)
        send_command(window.controller, 'RX')
        self.assertEqual(window.receiver_state.known, known)
        send_command(window.controller, 'AM')
        self.assertEqual(window.receiver_state.known, {})

    def test_external_setting_write_invalidates_only_that_setting(self):
        state = BandscopeReceiverState()
        state.confirm(parse_bandscope_status(STATUS))
        state.writing(b'SW5\r\n')
        self.assertNotIn('span_code', state.known)
        self.assertEqual(state.known['centre_hz'], 93500000)
        self.assertFalse(state.known['peak_hold'])


class CentreConfigurationTests(unittest.TestCase):
    def test_stopped_centre_updates_preview_clears_all_data_and_preserves_controls(self):
        window = Window()
        window.select_mode('Average')
        window.samples = (5,) * 128
        window.trace_status = dict(window.status)
        window.statistic.add_sweep(window.samples, window.status)
        window.spectrum.add_sweep(window.samples, window.status)
        window.span_choice.SetSelection(4)  # 500 kHz
        window.threshold.SetValue(8)
        window.spectrum_scale.SetSelection(1)
        window.centre_input.GetValue.return_value = '27.2'
        confirmed = dict(window.receiver_state.known)
        window.on_centre(None)
        self.assertEqual(window.commands, [])
        self.assertIsNone(window.samples)
        self.assertIsNone(window.trace_status)
        self.assertEqual(window.statistic.sweeps, 0)
        self.assertEqual(len(window.spectrum.history), 0)
        self.assertEqual(window.receiver_state.known, confirmed)
        self.assertEqual(window.status['centre_hz'], 93500000)
        self.assertEqual(window.display_geometry()['centre_hz'], 27200000)
        self.assertEqual((window.view.frequency_low, window.view.frequency_high), (26950000, 27450000))
        self.assertEqual(window.capture_run_config().centre_hz, 27200000)
        self.assertEqual((window.sweep_mode, window.span_choice.GetSelection(), window.threshold.GetValue(),
                          window.average_window.GetSelection(), window.spectrum_scale.GetSelection()),
                         ('Average', 4, 8, 3, 1))
        window.on_reset_zoom(None)
        self.assertEqual((window.view.frequency_low, window.view.frequency_high), (26950000, 27450000))

    def test_stopped_centre_and_span_are_available_offline_without_entering_analyser(self):
        window = Window()
        window.controller.connected = False
        window.status = None
        window.entered = False
        window.centre_input.GetValue.return_value = '27.2'
        window.on_centre(None)
        window.span_choice.SetSelection(4)
        window.on_span(None)
        self.assertEqual(window.commands, [])
        self.assertIsNone(window.run_config)
        self.assertFalse(window.running)
        self.assertFalse(window.entered)
        self.assertEqual(window.display_geometry()['span_hz'], 500000)
        self.assertEqual((window.view.frequency_low, window.view.frequency_high), (26950000, 27450000))

    def test_local_set_matching_confirmed_centre_does_not_send_cf_on_start(self):
        window = Window()
        window.on_centre(None)
        self.assertEqual(window.commands, [])
        window.start_sweep()
        self.assertEqual(window.commands, [b'AM\r\n', b'DS\r\n'])

    def test_running_centre_drains_ds_then_confirms_cf_without_changing_run_parameters(self):
        for mode in Window().SWEEP_MODES:
            with self.subTest(mode=mode):
                window = Window()
                window.select_mode(mode)
                window.start_sweep(peak=mode == 'Peak Hold')
                original = window.run_config
                window.add_ds_line('DS0127:' + '5' * 32)
                window.samples = (5,) * 128
                window.trace_status = dict(window.status)
                window.centre_input.GetValue.return_value = '94.25'
                # These passive edits remain next-Start values, not part of Set centre.
                window.span_choice.SetSelection(4)
                window.on_span(None)
                window.mode_radio.SetSelection(0)
                window.on_sweep_mode(None)
                window.threshold.SetValue(12)
                window.average_window.SetSelection(6)
                before = list(window.commands)
                window.on_centre(None)
                self.assertEqual(window.commands, before)
                self.assertTrue(window.running)
                self.assertEqual(window.waiting, 'data')
                window.add_ds_line('DS0095:' + '5' * 32)
                window.add_ds_line('DS0063:' + '5' * 32)
                window.add_ds_line('DS0031:' + '5' * 32)
                self.assertEqual(window.commands[len(before):], [b'CF0094250000\r\nAM\r\n'])
                self.assertIsNone(window.samples)
                self.assertEqual(window.statistic.sweeps, 0)
                self.assertEqual(len(window.spectrum.history), 0)
                self.assertEqual(window.run_config, replace(original, centre_hz=94250000))
                self.assertEqual(window.waiting, 'status')
                status = STATUS.replace('CF0093500000', 'CF0094250000')
                if mode == 'Peak Hold':
                    status = status.replace('PH0', 'PH1')
                window.set_status(status)
                self.assertEqual(window.receiver_state.known['centre_hz'], 94250000)
                self.assertEqual(window.commands[-1], b'DS\r\n')
                self.assertEqual(window.waiting, 'data')
                self.assertTrue(window.running)
                self.assertEqual(window.view.geometry, (94250000, 100000))
                self.assertNotIn(b'VF', lines(window))

    def test_running_centre_between_sweeps_applies_without_stop_start(self):
        window = Window()
        window.select_mode('Loop')
        window.start_sweep()
        window.finish_sweep()
        self.assertIsNone(window.waiting)
        window.commands.clear()
        window.centre_input.GetValue.return_value = '94.25'
        window.on_centre(None)
        self.assertEqual(window.commands, [b'CF0094250000\r\nAM\r\n'])
        self.assertTrue(window.entered)
        self.assertEqual(window.run_config.mode, 'Loop')

    def test_stop_cancels_queued_centre_without_applying_it(self):
        window = Window()
        window.select_mode('Loop')
        window.start_sweep()
        window.centre_input.GetValue.return_value = '94.25'
        window.on_centre(None)
        window.on_stop(None)
        window.commands.clear()
        window.finish_sweep()
        self.assertEqual(window.commands, [b'VF\r\nRX\r\n'])
        self.assertIsNone(window.pending_centre_hz)


if __name__ == '__main__':
    unittest.main()
