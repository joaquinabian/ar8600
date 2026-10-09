import wx
import wx.dataview as dv
import sys
import csv
import os
import tempfile
import serial_conf_dialog
import serial
import threading
import re
import time
from collections import deque
from datetime import datetime
from aor_control_frame import AorCtrlFrame
from aor_functions import (do_nothing, format_frequency, display_frequency, display_step,
                           protocol_frequency_hz, protocol_step_hz, parse_level_squelch_response,
                           parse_lm_response, parse_lc_response,
                           parse_select_scan_response, parse_pass_frequency_response,
                           make_pass_frequency_command, format_activity_row,
                           parse_bandscope_status, BandscopeSweep, bandscope_frequency,
                           BANDSCOPE_SPAN_LABELS, bandscope_centre_command, bandscope_marker_frequency)
from aor_functions import (search_parameters, make_search_bank_command, parse_search_bank_response,
                           parse_group_members, make_group_members_command,
                           parse_operation_parameter, operation_parameter_command,
                           SCAN_FILTER_MODES, SCAN_FILTER_CODES,
                           make_memory_channel_command, memory_channel_read_command)


# GUI order: WFM, NFM, SFM, WAM, AM, NAM, USB, LSB, CW.
GUI_MODE_TO_MD = (0, 1, 6, 7, 2, 8, 3, 4, 5)
MD_TO_GUI_MODE = {code: index for index, code in enumerate(GUI_MODE_TO_MD)}


DEBUG_SERIAL = False
LIST_LABELS = {'SEARCH BANKS': 'STORED RANGES', 'SELECT SCAN': 'SELECTED CHANNELS',
               'PASS FREQS': 'PASS FREQS', 'LOG VIEW': 'LOG VIEW', 'DATABASE': 'DATABASE'}


# Create an own event type, so that GUI updates can be delegated
SERIALRX = wx.NewEventType()
# bind to serial data receive events
EVT_SERIALRX = wx.PyEventBinder(SERIALRX, 0)


class SerialRxEvent(wx.PyCommandEvent):
    """"""
    eventType = SERIALRX

    def __init__(self, win_id, data):
        wx.PyCommandEvent.__init__(self, self.eventType, win_id)
        self.data = data

    # noinspection PyMethodOverriding
    def Clone(self):
        return self.__class__(self.GetId(), self.data)


class BandscopeWindow(wx.Frame):
    """A single in-flight sweep, delivered by the application's existing reader."""
    def __init__(self, controller):
        super().__init__(controller, title='AR8600 BAND SCOPE', size=(800, 430))
        self.controller = controller
        self.status = None
        self.samples = None
        self.trace_status = None
        self.sweep_status = None
        self.sweep_requested = False
        self.sweep = None
        self.waiting = None
        self.running = False
        self.entered = False
        self.restore_command = None
        self.close_requested = False
        self.started_at = None
        self.last_duration = None
        self.stop_requested = False
        self.pending_tune = None
        self.setting_command = b''
        self.peak_value = None
        self.peak_pending = False
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.start_button = wx.Button(panel, label='Start')
        self.stop_button = wx.Button(panel, label='Stop')
        self.loop_checkbox = wx.CheckBox(panel, label='Loop')
        self.peak_checkbox = wx.CheckBox(panel, label='Peak Hold')
        for button in (self.start_button, self.stop_button, self.loop_checkbox, self.peak_checkbox):
            buttons.Add(button, 0, wx.ALL, 5)
        buttons.Add(wx.StaticText(panel, label='Span:'), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 10)
        self.span_choice = wx.Choice(panel, choices=BANDSCOPE_SPAN_LABELS)
        self.span_choice.SetSelection(wx.NOT_FOUND)
        self.span_choice.Disable()
        buttons.Add(self.span_choice, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        sizer.Add(buttons)
        centre_controls = wx.BoxSizer(wx.HORIZONTAL)
        centre_controls.Add(wx.StaticText(panel, label='Centre (MHz):'), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.centre_input = wx.TextCtrl(panel, size=(120, -1), style=wx.TE_PROCESS_ENTER)
        self.centre_button = wx.Button(panel, label='Set centre')
        self.tune_button = wx.Button(panel, label='Tune to Marker')
        self.centre_input.Disable()
        self.centre_button.Disable()
        centre_controls.Add(self.centre_input, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        centre_controls.Add(self.centre_button, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        centre_controls.Add(self.tune_button, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        sizer.Add(centre_controls)
        self.info = wx.StaticText(panel, label='Centre: ---    Span: ---    Marker: ---')
        sizer.Add(self.info, 0, wx.ALL, 5)
        self.plot = wx.Panel(panel)
        self.plot.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.plot.Bind(wx.EVT_PAINT, self.on_paint)
        self.plot.Bind(wx.EVT_SIZE, self.on_plot_size)
        self.plot.Bind(wx.EVT_LEFT_DOWN, self.on_marker_click)
        sizer.Add(self.plot, 1, wx.EXPAND | wx.ALL, 5)
        self.message = wx.StaticText(panel, label='Start reads one sweep. Check Loop for continuous sweeps; Stop restores receiver audio.')
        sizer.Add(self.message, 0, wx.ALL, 5)
        panel.SetSizer(sizer)
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_timer, self.timer)
        self.start_button.Bind(wx.EVT_BUTTON, self.on_start)
        self.stop_button.Bind(wx.EVT_BUTTON, self.on_stop)
        self.tune_button.Bind(wx.EVT_BUTTON, self.on_tune_marker)
        self.peak_checkbox.Bind(wx.EVT_CHECKBOX, self.on_peak_hold)
        self.span_choice.Bind(wx.EVT_CHOICE, self.on_span)
        self.centre_button.Bind(wx.EVT_BUTTON, self.on_centre)
        self.centre_input.Bind(wx.EVT_TEXT_ENTER, self.on_centre)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        for control, tip in (
                (self.start_button, 'Read one sweep, then restore audio; Loop repeats until Stop.'),
                (self.stop_button, 'Finish any pending sweep and restore the previous receiver state/audio.'),
                (self.loop_checkbox, 'Keep analyser mode active and repeat completed sweeps until Stop.'),
                (self.peak_checkbox, 'Enable scanner bandscope peak hold; read back the PH state.'),
                (self.tune_button, 'Finish any pending sweep, restore audio, tune the active VFO to MF, then read RX.'),
                (self.span_choice, 'Set the bandscope width and clear the previous trace.'),
                (self.centre_input, 'Enter the bandscope centre frequency in MHz.'),
                (self.centre_button, 'Set the bandscope centre; Start reads a new trace.'),
                (self.plot, 'Click to move the bandscope marker without tuning the receiver.')):
            control.SetToolTip(tip)
        self.set_controls_enabled(True)

    @property
    def pauses_lm(self):
        return self.waiting is not None and (self.sweep is None or not self.sweep.failed)

    def available(self):
        return (self.controller.connected and self.controller.serial.is_open and self.controller.alive.is_set()
                and self.controller.memory_inventory is None)

    def send(self, data):
        if self.controller.write_serial(data) == len(data):
            return True
        self.disconnect()
        self.message.SetLabel('Bandscope command failed; see serial diagnostics.')
        return False

    def on_start(self, event):
        if self.waiting is not None:
            return
        self.running = True
        self.stop_requested = False
        self.close_requested = False
        self.request_sweep()

    def on_stop(self, event):
        self.running = False
        self.stop_requested = True
        if self.waiting is not None:
            self.message.SetLabel('Stopping after current sweep' if self.waiting == 'data' else 'Restoring receiver')
        else:
            self.timer.Stop()
            self.restore_receiver()

    def on_tune_marker(self, event):
        if not self.available() or self.status is None or self.waiting == 'restore':
            return
        self.pending_tune = self.status['marker_hz']
        self.on_stop(event)

    def request_sweep(self):
        command = b''
        if not self.entered and self.status is not None:
            # This receiver resets AM geometry on entry; retain the viewer's settings.
            command = ('CF%010d\r\nSW%d\r\nMF%010d\r\n' %
                       (self.status['centre_hz'], self.status['span_code'], self.status['marker_hz'])).encode('ascii')
        self.request_status(command, sweep=True)

    def set_controls_enabled(self, enabled):
        self.start_button.Enable(enabled and self.available())
        self.stop_button.Enable(self.available())
        self.loop_checkbox.Enable(self.available())
        self.peak_checkbox.Enable(enabled and self.available() and self.peak_value is not None and not self.peak_pending)
        self.tune_button.Enable(self.available() and self.status is not None and self.waiting != 'restore')
        settings_enabled = enabled and self.status is not None and self.available()
        self.span_choice.Enable(settings_enabled)
        self.centre_input.Enable(settings_enabled)
        self.centre_button.Enable(settings_enabled)

    def request_status(self, command=b'', sweep=False):
        if not self.available():
            self.running = False
            self.message.SetLabel('Connect to the scanner before requesting a sweep.')
            return
        if self.waiting is not None:
            return
        self.stop_requested = False
        self.sweep = None
        self.setting_command = command
        self.sweep_requested = sweep
        self.waiting = 'status' if self.entered else 'receiver'
        self.set_controls_enabled(False)
        self.started_at = time.monotonic()
        # One-shot watchdog reports failure; it never sends another DS request.
        self.timer.StartOnce(75000)
        self.message.SetLabel('Reading bandscope status (LM polling paused).')
        self.send(command + b'AM\r\n' if self.entered else b'RX\r\n')

    def change_setting(self, command, invalidate=False):
        if self.waiting is not None or self.status is None or not self.available():
            return
        if invalidate:
            self.samples = None
            self.trace_status = None
            self.plot.Refresh()
        if not self.entered:
            command = ('CF%010d\r\nSW%d\r\nMF%010d\r\n' %
                       (self.status['centre_hz'], self.status['span_code'], self.status['marker_hz'])).encode('ascii') + command
        self.request_status(command, sweep=self.running)

    def on_span(self, event):
        selection = self.span_choice.GetSelection()
        if 0 <= selection < len(BANDSCOPE_SPAN_LABELS):
            self.change_setting(('SW%d\r\n' % (selection + 1)).encode('ascii'), invalidate=True)

    def on_peak_hold(self, event):
        desired = self.peak_checkbox.GetValue()
        self.peak_checkbox.SetValue(bool(self.peak_value))
        if self.waiting is not None or not self.available() or self.peak_value is None or self.peak_pending:
            return
        self.peak_pending = True
        self.change_setting(('PH%d\r\nPH\r\n' % desired).encode('ascii'))

    def set_peak_hold(self, text):
        if re.fullmatch(r'PH[01]', text) is None:
            raise ValueError('Invalid peak hold response')
        self.peak_value = text == 'PH1'
        self.peak_pending = False
        self.peak_checkbox.SetValue(self.peak_value)
        if self.status is not None:
            self.status['peak_hold'] = self.peak_value
        self.set_controls_enabled(self.waiting is None)

    def on_centre(self, event):
        if self.waiting is not None or not self.available():
            return
        try:
            command = bandscope_centre_command(self.centre_input.GetValue().strip())
        except ValueError as error:
            self.message.SetLabel(str(error))
            return
        self.change_setting(command, invalidate=True)

    def on_marker_click(self, event):
        if self.waiting is not None or self.status is None or not self.available():
            return
        width, height = self.plot.GetClientSize()
        left, top, right, bottom = 55, 20, width - 20, height - 45
        x, y = event.GetPosition()
        if right <= left or not left <= x <= right or not top <= y <= bottom:
            return
        low = self.status['centre_hz'] - self.status['span_hz'] / 2
        target = low + (x - left) * self.status['span_hz'] / (right - left)
        try:
            frequency = bandscope_marker_frequency(self.status, target)
        except ValueError as error:
            self.message.SetLabel(str(error))
            return
        self.change_setting(('MF%010d\r\n' % frequency).encode('ascii'))

    def capture_receiver(self, text):
        restored = text.startswith(('VA ', 'VB ', 'VF ', 'MR MX', 'MS MX', 'SM MX', 'VS V')) or re.match(r'SR[A-Ta-t] RF', text)
        if self.waiting == 'restore' and restored:
            if text.startswith(('VA ', 'VB ', 'VF ')):
                self.controller.validate_fields(text.split()[1:], ('RF', 'ST', 'AU', 'MD', 'AT'))
            self.waiting = None
            self.timer.Stop()
            self.set_controls_enabled(True)
            self.message.SetLabel('Sweep complete — receiver restored' if self.samples is not None else 'Receiver restored')
            return
        if self.waiting != 'receiver':
            return
        fields = text.split()
        if not fields:
            return
        context = fields[0]
        if context in ('VA', 'VB', 'VF'):
            self.controller.validate_fields(fields[1:], ('RF', 'ST', 'AU', 'MD', 'AT'))
            self.restore_command = (context + '\r\n').encode('ascii')
        elif context in ('MR', 'MS', 'SM'):
            match = re.match(r'(?:MR|MS|SM) MX([A-Ja-j][0-9]{2}) ', text)
            if not match:
                return
            self.restore_command = ('MR%s\r\n' % match.group(1) if context == 'MR' else
                                    'MS%s\r\n' % match.group(1)[0] if context == 'MS' else 'SM\r\n').encode('ascii')
        elif re.fullmatch(r'SR[A-Ta-t]', context) and ' RF' in text:
            self.restore_command = ('SS%s\r\n' % context[2]).encode('ascii')
        elif context == 'VS':
            self.restore_command = b'VS\r\n'
        else:
            return
        if self.stop_requested:
            self.waiting = None
            self.restore_receiver()
            return
        self.waiting = 'status'
        self.entered = True
        # The first AM enters; the second obtains the documented status.
        self.peak_pending = True
        peak_query = b'' if b'PH\r\n' in self.setting_command else b'PH\r\n'
        self.send(b'AM\r\n' + self.setting_command + b'AM\r\n' + peak_query)

    def set_status(self, text):
        status = parse_bandscope_status(text)
        if self.waiting == 'receiver':  # The scanner was already in analyser mode.
            self.entered = True
            self.waiting = 'status'
            self.restore_command = self.controller.normal_vfo_command()
            self.send(b'PH\r\n')
        if self.waiting != 'status':
            return
        if self.status is None or any(self.status[field] != status[field] for field in ('centre_hz', 'span_code')):
            self.samples = None  # Never relabel an old trace with new geometry.
            self.trace_status = None
            self.plot.Refresh()
        self.status = status
        self.peak_value = status['peak_hold']
        self.peak_pending = False
        self.peak_checkbox.SetValue(self.peak_value)
        self.span_choice.SetSelection(status['span_code'] - 1)
        self.centre_input.ChangeValue('%.6f' % (status['centre_hz'] / 1000000))
        self.info.SetLabel('Centre: %.6f MHz    Span: %g MHz    Marker: %.6f MHz%s' %
                           (status['centre_hz'] / 1000000, status['span_hz'] / 1000000,
                            status['marker_hz'] / 1000000, '    Peak hold' if status['peak_hold'] else ''))
        self.plot.Refresh()
        if not self.sweep_requested or self.stop_requested:
            self.waiting = None
            self.timer.Stop()
            self.restore_receiver()
            return
        # Real narrow-span dumps contain 128 points; retain manual-format support.
        self.sweep = BandscopeSweep((128, 1024) if status['span_code'] in (6, 7) else (1024,))
        self.sweep_status = status.copy()
        self.waiting = 'data'
        self.started_at = time.monotonic()
        self.timer.StartOnce(75000)
        self.message.SetLabel('Continuous sweep — receiver audio unavailable' if self.loop_checkbox.IsChecked()
                              else 'Sweeping — receiver audio unavailable')
        self.send(b'DS\r\n')

    def add_ds_line(self, text):
        if self.waiting != 'data':
            return  # Ignore late data after disconnect; do not populate ordinary lists.
        try:
            samples = self.sweep.add_line(text)
        except ValueError as error:
            print('Bandscope DS error %r: %s' % (text, error), file=sys.stderr)
            self.message.SetLabel('Invalid sweep: %s' % error)
            self.running = False
            samples = None
        if samples is not None:
            self.samples = samples
            self.trace_status = self.sweep_status
            self.last_duration = time.monotonic() - self.started_at
            self.message.SetLabel('%d samples received in %.2f s. 0: unmeasured; 1: outside specification; 2-F: raw level.' %
                                  (len(samples), self.last_duration))
            self.plot.Refresh()
        elif not self.sweep.failed:
            self.message.SetLabel('Stopping after current sweep' if self.stop_requested else
                                  'Sweeping — receiver audio unavailable (%d samples)' % (len(self.sweep.blocks) * 32))
        if self.sweep.finished:
            if self.sweep.failed:
                self.message.SetLabel('Sweep rejected: incomplete or malformed data.')
            self.waiting = None
            self.timer.Stop()
            self.set_controls_enabled(True)
            if self.running and self.loop_checkbox.IsChecked() and not self.stop_requested:
                self.message.SetLabel('Continuous sweep')
                self.timer.StartOnce(500)
            else:
                self.running = False
                self.restore_receiver()

    def on_timer(self, event):
        if self.waiting is None:
            if self.running:
                if self.loop_checkbox.IsChecked():
                    self.request_sweep()
                else:
                    self.running = False
                    self.restore_receiver()
            return
        self.running = False
        if self.waiting == 'data':
            self.sweep.failed = True
            self.message.SetLabel('Sweep timed out; LM resumed. Waiting for DS end; reconnect if it never arrives.')
            # Retain the outstanding request to prevent overlap with late data.
        else:
            self.waiting = None
            self.set_controls_enabled(True)
            self.message.SetLabel('No valid bandscope status received; restoring receiver.')
            self.restore_receiver()
        print('Bandscope request timed out', file=sys.stderr)

    def restore_receiver(self, query=True):
        self.timer.Stop()
        command = self.restore_command or self.controller.normal_vfo_command()
        tune = self.pending_tune
        if self.controller.serial.is_open and (self.entered or tune is not None):
            if tune is not None:
                vfo = self.controller.normal_vfo_command()
                if command != vfo:
                    command += vfo
                command += ('RF%010d\r\n' % tune).encode('ascii')
            if query:
                command += b'RX\r\n'
                self.waiting = 'restore'
                self.timer.StartOnce(5000)
            self.controller.write_serial(command)
            self.message.SetLabel('Restoring receiver' if query else 'Receiver restored')
        else:
            self.message.SetLabel('Receiver restored')
        self.pending_tune = None
        self.entered = False
        self.restore_command = None
        self.set_controls_enabled(self.waiting is None)

    def disconnect(self):
        self.running = False
        self.stop_requested = True
        self.pending_tune = None
        self.timer.Stop()
        self.restore_receiver(query=False)
        self.waiting = None
        self.sweep = None
        self.peak_value = None
        self.peak_pending = False
        self.peak_checkbox.SetValue(False)
        self.set_controls_enabled(True)
        self.message.SetLabel('Disconnected.')

    def on_close(self, event):
        self.on_stop(event)
        self.close_requested = True
        self.Hide()  # Retain pending sweep state until its last line is drained.

    def on_plot_size(self, event):
        self.plot.Refresh()
        event.Skip()

    def on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self.plot)
        dc.SetBackground(wx.Brush('white'))
        dc.Clear()
        width, height = self.plot.GetClientSize()
        left, top, right, bottom = 55, 20, width - 20, height - 45
        if right <= left or bottom <= top:
            return
        dc.SetTextForeground('black')
        dc.SetPen(wx.Pen('#dddddd'))
        for level in (0, 2, 5, 10, 15):
            y = bottom - round(level * (bottom - top) / 15)
            dc.DrawLine(left, y, right, y)
            dc.DrawText('%X' % level, 25, y - 7)
        dc.DrawText('Raw', 12, 0)
        dc.SetPen(wx.Pen('black'))
        dc.DrawLine(left, top, left, bottom)
        dc.DrawLine(left, bottom, right, bottom)
        if self.status is None:
            dc.DrawText('No bandscope status yet', left + 15, top + 15)
            return
        status = self.status
        low = status['centre_hz'] - status['span_hz'] / 2
        high = status['centre_hz'] + status['span_hz'] / 2
        def x_at(frequency):
            return left + round((frequency - low) * (right - left) / (high - low))
        for n in range(5):
            frequency = low + n * (high - low) / 4
            x = x_at(frequency)
            label = '%.3f' % (frequency / 1000000)
            dc.DrawText(label, x - dc.GetTextExtent(label)[0] // 2, bottom + 6)
        dc.DrawText('Frequency (MHz)', left + (right - left) // 2 - 50, bottom + 24)
        dc.SetPen(wx.Pen('#aa3333', 1, wx.PENSTYLE_SHORT_DASH))
        marker_x = x_at(status['marker_hz'])
        if low <= status['marker_hz'] <= high:
            dc.DrawLine(marker_x, top, marker_x, bottom)
            label = '%.3f MHz' % (status['marker_hz'] / 1000000)
            dc.DrawText(label, min(marker_x + 4, right - dc.GetTextExtent(label)[0]), top)
        if self.samples is None:
            return
        dc.SetPen(wx.Pen('#1565b0', 1))
        previous = None
        for index, level in enumerate(self.samples):
            frequency = bandscope_frequency(self.trace_status, index)
            if not low <= frequency <= high or level < 2:
                previous = None  # 0/1 are validity codes, not measured signal levels.
                continue
            point = (x_at(frequency), bottom - round(level * (bottom - top) / 15))
            if previous is None:
                dc.DrawPoint(*point)
            else:
                dc.DrawLine(*previous, *point)
            previous = point


class GroupDialog(wx.Dialog):
    """Read back the scanner's membership; Save is an explicit replacement."""
    def __init__(self, controller, kind, group):
        label = 'Linked Memory Banks' if kind == 'scan' else 'Linked Ranges'
        super().__init__(controller, title='%s (%s Group %d)' % (label, kind.title(), group), size=(370, 440))
        self.controller, self.kind, self.group = controller, kind, group
        self.confirmed_members = None
        self.expected_members = None
        self.verification_members = None
        self.banks = list('ABCDEFGHIJabcdefghij' if kind == 'scan' else 'ABCDEFGHIJKLMNOPQRSTabcdefghijklmnopqrst')
        labels = []
        for bank in self.banks:
            metadata = controller.memory_banks.get(bank) if kind == 'scan' else controller.search_banks.get(bank)
            name = metadata.get('name', '').strip() if metadata else ''
            labels.append(bank + (' — ' + name if name and name != bank else ''))
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.message = wx.StaticText(self, label='Reading %s Group from scanner...' % kind.title())
        self.message.Wrap(320)
        instructions = wx.StaticText(self, label='Check boxes to link %s.\nHighlighting a row does not change its checkbox.' %
                                     ('Memory Banks' if kind == 'scan' else 'Search Banks'))
        self.members = wx.CheckListBox(self, choices=labels)
        self.members.Disable()
        self.members.SetToolTip('Memory Banks scanned together.' if kind == 'scan' else 'Stored Search Banks searched together.')
        self.save = wx.Button(self, label='Save membership')
        self.save.Disable()
        sizer.Add(self.message, 0, wx.ALL, 8)
        sizer.Add(instructions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sizer.Add(self.members, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self.save, 0, wx.ALL, 8)
        self.SetSizer(sizer)
        self.save.SetToolTip('Replace group links with these checked banks, then read back the scanner configuration.')
        self.save.Bind(wx.EVT_BUTTON, self.on_save)
        self.members.Bind(wx.EVT_CHECKLISTBOX, self.on_members_changed)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.read_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_read_timeout, self.read_timer)
        self.read_timer.StartOnce(5000)
        if group == 0:
            self.set_message('LINK OFF — fixed, read-only group')

    def proposed_members(self):
        return tuple(self.banks[index] for index in self.members.GetCheckedItems())

    def set_message(self, message, error=False):
        if self.group == 0 and not message.startswith('LINK OFF'):
            message = 'LINK OFF — fixed, read-only group\n' + message
        self.message.SetForegroundColour(wx.RED if error else wx.NullColour)
        self.message.SetLabel(message)
        self.message.Wrap(320)
        self.Layout()

    def on_members_changed(self, event):
        self.verification_members = None
        self.update_save_state()
        if self.confirmed_members is not None:
            self.set_message('No changes to save' if self.proposed_members() == self.confirmed_members else
                             'Proposed links: %s. Press Save membership.' % (', '.join(self.proposed_members()) or '(none)'))

    def update_save_state(self):
        self.save.Enable(self.group != 0 and self.controller.connected and
                         self.confirmed_members is not None and self.expected_members is None and
                         set(self.proposed_members()) != set(self.confirmed_members))

    def show_members(self, members):
        self.read_timer.Stop()
        self.confirmed_members = tuple(members)
        self.members.SetCheckedItems([index for index, bank in enumerate(self.banks) if bank in members])
        self.members.Enable(self.group != 0)
        self.save.Disable()
        if self.expected_members is not None or self.verification_members is not None:
            expected = self.expected_members if self.expected_members is not None else self.verification_members
            self.expected_members = None
            # BM/BS replacement can itself reply, before the explicit read-back.
            # Compare both replies and retain the result instead of erasing it.
            self.verification_members = expected
            matches = set(members) == set(expected)
            self.set_message('%s Group %d saved and verified' % (self.kind.title(), self.group) if matches else
                             'Save verification failed. Scanner returned: %s; requested: %s' %
                             (', '.join(members) or '(none)', ', '.join(expected) or '(none)'), error=not matches)
            self.controller.aor_status.SetStatusText(self.message.GetLabel().replace('\n', ' '))
        else:
            self.set_message('LINK OFF — fixed, read-only group' if self.group == 0 else
                             'Scanner links: ' + (', '.join(members) or '(none)'))

    def on_save(self, event):
        if self.group == 0 or not self.controller.connected or self.confirmed_members is None or self.expected_members is not None:
            return
        if self.controller.group_loading[self.kind] is not None:
            self.set_message('Wait for the current group read-back before saving.')
            return
        members = self.proposed_members()
        if members == self.confirmed_members:
            self.verification_members = None
            self.set_message('No changes to save')
            return
        command = make_group_members_command(self.kind, self.group, members)
        self.save.Disable()
        self.members.Disable()
        self.expected_members = members
        self.set_message('Saving and verifying scanner links...')
        self.controller.group_loading[self.kind] = self.group
        # The editor has already read and identified this group; BM/BS is the
        # explicit read-back for the replacement, rather than an unrelated GM/GS query.
        self.controller.group_response[self.kind] = self.group
        if self.controller.write_serial(command) != len(command):
            self.expected_members = None
            self.controller.group_loading[self.kind] = None
            self.members.Enable(True)
            self.update_save_state()
            self.set_message('Serial write failed; membership was not verified.', error=True)
        else:
            self.read_timer.StartOnce(5000)
        self.controller.update_connection_ui()

    def on_read_timeout(self, event):
        self.expected_members = None
        self.verification_members = None
        self.confirmed_members = None
        self.save.Disable()
        self.members.Disable()
        self.controller.group_loading[self.kind] = None
        self.set_message('No group read-back received. Close and reopen the editor to read again.', error=True)
        self.controller.update_connection_ui()

    def on_close(self, event):
        self.read_timer.Stop()
        self.controller.group_dialogs[self.kind] = None
        self.Destroy()


class MemoryChannelDialog(wx.Dialog):
    def __init__(self, controller, channel=None):
        super().__init__(controller, title='Edit Channel' if channel else 'New Channel')
        self.controller = controller
        self.existing = channel is not None
        self.channel = wx.Choice(self, choices=[channel] if channel else sorted(controller.memory_empty_channels))
        self.channel.SetSelection(0)
        self.frequency = wx.TextCtrl(self)
        self.auto = wx.CheckBox(self)
        self.mode = wx.Choice(self, choices=controller.cbx_mode.GetItems())
        self.mode.SetSelection(0)
        self.step = wx.ComboBox(self, value='100', choices=controller.cbx_step.GetItems())
        self.attenuator = wx.CheckBox(self)
        self.name = wx.TextCtrl(self)
        self.name.SetMaxLength(12)
        self.controls = (self.channel, self.frequency, self.auto, self.mode, self.step, self.attenuator, self.name)
        grid = wx.FlexGridSizer(0, 2, 6, 8)
        for label, control, tip in (
                ('Channel', self.channel, 'Memory location within the displayed bank; existing channels are not moved.'),
                ('Frequency MHz', self.frequency, 'Stored receive frequency in MHz.'),
                ('Auto', self.auto, 'Let the AR8600 bandplan choose the stored mode and step.'),
                ('Mode', self.mode, 'Stored modulation when Auto is off.'),
                ('Step kHz', self.step, 'Stored tuning step when Auto is off.'),
                ('Attenuator', self.attenuator, 'Store attenuation on or off for this channel.'),
                ('Name', self.name, 'Up to 12 printable ASCII characters.')):
            grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(control, 0, wx.EXPAND)
            control.SetToolTip(tip)
        self.message = wx.StaticText(self, label='Selected and Skip are retained when editing.', size=(350, 55))
        self.save = wx.Button(self, label='Save to scanner')
        close = wx.Button(self, wx.ID_CLOSE, 'Close')
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.Add(self.save, 0, wx.RIGHT, 6)
        buttons.Add(close)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(grid, 0, wx.EXPAND | wx.ALL, 10)
        sizer.Add(self.message, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        sizer.Add(buttons, 0, wx.ALL, 10)
        self.SetSizerAndFit(sizer)
        self.auto.SetValue(True)
        if channel:
            self.show_channel(controller.memory_rows[channel]['fields'])
        self.set_pending(False)
        self.auto.Bind(wx.EVT_CHECKBOX, lambda event: self.set_pending(False))
        self.save.Bind(wx.EVT_BUTTON, self.on_save)
        close.Bind(wx.EVT_BUTTON, lambda event: self.Close())
        self.Bind(wx.EVT_CLOSE, self.on_close)

    def set_pending(self, pending):
        for control in self.controls:
            control.Enable(not pending)
        self.channel.Enable(not pending and not self.existing)
        self.mode.Enable(not pending and not self.auto.GetValue())
        self.step.Enable(not pending and not self.auto.GetValue())
        self.save.Enable(not pending and self.controller.connected)

    def show_channel(self, fields):
        self.frequency.ChangeValue('%.6f' % (protocol_frequency_hz(fields[2]) / 1000000))
        self.step.SetValue('%g' % (protocol_step_hz(fields[3]) / 1000))
        self.auto.SetValue(fields[4] == '1')
        self.mode.SetSelection(MD_TO_GUI_MODE[int(fields[5])])
        self.attenuator.SetValue(fields[6] == '1')
        self.name.ChangeValue(fields[7])

    def on_save(self, event):
        if not self.save.IsEnabled():
            return
        try:
            if not 0 <= self.mode.GetSelection() < len(GUI_MODE_TO_MD):
                raise ValueError('Choose a modulation mode')
            command = make_memory_channel_command(self.channel.GetStringSelection(), self.frequency.GetValue(),
                                                  self.auto.GetValue(), GUI_MODE_TO_MD[self.mode.GetSelection()],
                                                  self.step.GetValue(), self.attenuator.GetValue(), self.name.GetValue())
        except (ValueError, IndexError) as error:
            self.message.SetLabel(str(error))
            return
        self.controller.save_memory_channel(self.channel.GetStringSelection(), command, new=not self.existing)

    def on_close(self, event):
        self.controller.memory_channel_dialog = None
        self.Destroy()


class SearchBankDialog(wx.Dialog):
    def __init__(self, controller, bank):
        super().__init__(controller, title='Create / Edit Stored Range (Search Bank)')
        self.controller = controller
        self.bank = wx.Choice(self, choices=list('ABCDEFGHIJKLMNOPQRSTabcdefghijklmnopqrst'))
        self.bank.SetStringSelection(bank)
        self.lower = wx.TextCtrl(self)
        self.upper = wx.TextCtrl(self)
        self.step = wx.TextCtrl(self, value='100')
        self.mode = wx.Choice(self, choices=['Auto'] + controller.cbx_mode.GetItems())
        self.mode.SetSelection(0)
        self.name = wx.TextCtrl(self)
        self.name.SetMaxLength(12)
        grid = wx.FlexGridSizer(0, 2, 6, 8)
        for label, control, tip in (
                ('Stored Range', self.bank, 'AR8600 Search Bank to create or edit; A-T or a-t.'),
                ('Lower MHz', self.lower, 'Lower limit of this stored frequency range.'),
                ('Upper MHz', self.upper, 'Upper limit, greater than Lower.'),
                ('Step kHz', self.step, 'Manual tuning step; Auto follows the scanner bandplan.'),
                ('Mode / Auto', self.mode, 'Choose modulation or the scanner automatic mode/step.'),
                ('Name', self.name, 'Up to 12 printable ASCII characters identifying this Search Bank.')):
            grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(control, 1, wx.EXPAND)
            control.SetToolTip(tip)
        self.message = wx.StaticText(self, label='Reading scanner values. Existing attenuation is retained.')
        self.save = wx.Button(self, label='Save to scanner')
        self.save.Disable()
        self.save.SetToolTip('Write this Search Bank with SE and read back SR; stored memories are unaffected.')
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(grid, 0, wx.ALL, 10)
        sizer.Add(self.message, 0, wx.ALL, 10)
        sizer.Add(self.save, 0, wx.ALL, 10)
        self.SetSizerAndFit(sizer)
        self.bank.Bind(wx.EVT_CHOICE, self.on_bank)
        self.save.Bind(wx.EVT_BUTTON, self.on_save)
        self.Bind(wx.EVT_CLOSE, self.on_close)

    def on_bank(self, event):
        self.save.Disable()
        self.message.SetLabel('Reading scanner values...')
        self.controller.edit_bank_pending = self.bank.GetStringSelection()
        self.controller.write_serial(('SR%s\r\n' % self.controller.edit_bank_pending).encode('ascii'))

    def show_bank(self, entry):
        if self.bank.GetStringSelection() != entry['bank']:
            return
        if entry['empty']:
            self.lower.ChangeValue('')
            self.upper.ChangeValue('')
            self.step.ChangeValue('100')
            self.mode.SetSelection(0)
            self.name.ChangeValue('')
        else:
            self.lower.ChangeValue('%.6f' % (entry['lower_hz'] / 1000000))
            self.upper.ChangeValue('%.6f' % (entry['upper_hz'] / 1000000))
            self.step.ChangeValue(format(entry['step_khz'], 'f'))
            self.mode.SetSelection(0 if entry['auto'] else MD_TO_GUI_MODE[entry['mode']] + 1)
            self.name.ChangeValue(entry['name'])
        self.save.Enable(self.controller.connected)
        self.message.SetLabel('Scanner values received. Attenuation is retained when saving.')

    def on_save(self, event):
        if not self.controller.connected or self.controller.bank_write_pending is not None:
            return
        selection = self.mode.GetSelection()
        code = None if selection == 0 else GUI_MODE_TO_MD[selection - 1]
        try:
            command = make_search_bank_command(self.bank.GetStringSelection(), self.lower.GetValue(),
                                               self.upper.GetValue(), self.step.GetValue(), code, self.name.GetValue())
        except ValueError as error:
            self.message.SetLabel(str(error))
            self.Fit()
            return
        bank = self.bank.GetStringSelection()
        self.controller.bank_write_pending = bank
        self.save.Disable()
        self.message.SetLabel('Write requested; waiting for scanner read-back...')
        self.controller.write_serial(command + ('SR%s\r\n' % bank).encode('ascii'))

    def on_close(self, event):
        self.controller.search_bank_dialog = None
        self.controller.edit_bank_pending = None
        self.Destroy()


# noinspection PyUnusedLocal
class OperationSettingsDialog(wx.Dialog):
    """One confirmed scanner context; Save writes changed fields and reads back."""
    def __init__(self, controller, family, group):
        super().__init__(controller, title='Operation Settings')
        self.controller, self.family, self.group = controller, family, group
        self.context_confirmed = family == 'D'
        self.values, self.expected = {}, None
        self.controls, self.options = {}, {}
        self.context_label = wx.StaticText(self, label='Reading scanner context...')
        self.message = wx.StaticText(self, label='Reading current settings...', size=(360, 45))
        grid = wx.FlexGridSizer(0, 2, 8, 10)
        fields = [('D', 'Resume delay', ['Off'] + (['Hold'] if family != 'X' else []) +
                   ['%.1f s' % (n / 10) for n in range(1, 100)],
                   [0] + (['Hold'] if family != 'X' else []) + list(range(1, 100)),
                   'Wait after the signal disappears and squelch closes before resuming. '+family+'D.'),
                  ('P', 'Maximum dwell', ['Off'] + ['%d s' % n for n in range(1, 61)],
                   list(range(61)), 'Continue after this time even if the signal remains active. '+family+'P / FREE.'),
                  ('B', 'Signal threshold', ['Off'] + [str(n) for n in range(1, 256)],
                   list(range(256)), 'Ignore signals below this raw level; 0 disables it. '+family+'B.'),
                  ('A', 'Voice threshold', ['Off'] + [str(n) for n in range(1, 256)],
                   list(range(256)), 'Require sufficient detected audio/voice; 0 disables it. '+family+'A.')]
        if family == 'X':
            fields.append(('M', 'Mode filter', SCAN_FILTER_MODES, SCAN_FILTER_CODES,
                           'AR8600 XM: scan all modes or only the selected modulation.'))
        for suffix, label, labels, values, tip in fields:
            key = family + suffix
            control = wx.Choice(self, choices=list(labels), size=(150, -1))
            control.SetToolTip(tip)
            control.Disable()
            self.controls[key], self.options[key] = control, list(values)
            control.Bind(wx.EVT_CHOICE, self.on_changed)
            grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(control, 0, wx.EXPAND)
        self.save = wx.Button(self, label='Save settings')
        self.refresh = wx.Button(self, label='Read settings')
        close = wx.Button(self, wx.ID_CLOSE, 'Close')
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        for button in (self.save, self.refresh, close):
            buttons.Add(button, 0, wx.RIGHT, 6)
        sizer = wx.BoxSizer(wx.VERTICAL)
        for item in (self.context_label, grid, self.message, buttons):
            sizer.Add(item, 0, wx.EXPAND | wx.ALL, 10)
        self.SetSizerAndFit(sizer)
        self.save.Disable()
        self.save.Bind(wx.EVT_BUTTON, self.on_save)
        self.refresh.Bind(wx.EVT_BUTTON, self.read)
        close.Bind(wx.EVT_BUTTON, lambda event: self.Close())
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_timeout, self.timer)

    def read(self, event=None):
        self.values.clear()
        self.context_confirmed = self.family == 'D'
        self.save.Disable()
        for control in self.controls.values():
            control.Disable()
        self.message.SetLabel('Reading scanner settings...')
        self.refresh.Disable()
        prefix = 'GM' if self.family == 'X' else 'GS'
        command = b''
        if self.family != 'D':
            command = (('%s%d\r\n' % (prefix, self.group)) if self.group is not None else '').encode('ascii')
            command += (prefix + '\r\n').encode('ascii')
        else:
            command = ''.join(key + '\r\n' for key in self.controls).encode('ascii')
        self.timer.StartOnce(5000)
        if self.controller.write_serial(command) != len(command):
            self.on_timeout(None)

    def accept_context(self, kind, group):
        if ('X' if kind == 'scan' else 'S') != self.family:
            return
        if self.group is not None and self.group != group:
            self.message.SetLabel('Scanner returned a different group; settings remain locked.')
            self.context_confirmed = False
            return
        self.group, self.context_confirmed = group, True
        self.update()

    def accept(self, key, value):
        if key not in self.controls:
            return
        self.values[key] = value
        # Do not invent a selectable value for an out-of-range scanner setting.
        self.controls[key].SetSelection(self.options[key].index(value) if value in self.options[key] else wx.NOT_FOUND)
        self.update()

    def update(self):
        complete = self.context_confirmed and set(self.values) == set(self.controls)
        self.context_label.SetLabel('Scan Group 0 — factory defaults, read only' if self.family == 'X' and self.group == 0 else
                                   'Applies to Manual Range / VFO (DB also controls Level Squelch)' if self.family == 'D' else
                                   'Applies to %s group %s%s' %
                                   ('Linked Memory Banks' if self.family == 'X' else 'Linked Ranges',
                                    self.group, ' - fixed, read-only' if self.group == 0 else ''))
        if not complete:
            return
        self.timer.Stop()
        self.refresh.Enable()
        for control in self.controls.values():
            control.Enable(self.family == 'D' or self.group != 0)
        if self.expected is not None:
            matched = all(self.values[key] == value for key, value in self.expected.items())
            self.message.SetLabel('Settings saved and verified.' if matched else
                                  'Verification failed; controls show scanner-returned values.')
            self.expected = None
        else:
            self.message.SetLabel('Current scanner settings.' if self.family == 'D' or self.group != 0 else
                                  'Group 0 is fixed and read-only.')
        self.on_changed(None)

    def proposed(self):
        if any(control.GetSelection() == wx.NOT_FOUND for control in self.controls.values()):
            raise ValueError('Choose a supported value for every setting before saving.')
        return {key: self.options[key][control.GetSelection()] for key, control in self.controls.items()}

    def on_changed(self, event):
        try:
            changed = self.proposed() != self.values
        except ValueError:
            changed = False
        self.save.Enable(self.context_confirmed and len(self.values) == len(self.controls) and
                         (self.family == 'D' or self.group != 0) and changed and self.expected is None)

    def on_save(self, event):
        if not self.save.IsEnabled():
            return
        proposed = self.proposed()
        changed = {key: value for key, value in proposed.items() if self.values[key] != value}
        command = b''.join(operation_parameter_command(key, value) for key, value in changed.items())
        if self.family != 'D':
            command = ('%s%d\r\n' % ('GM' if self.family == 'X' else 'GS', self.group)).encode('ascii') + command
        self.expected = proposed
        if self.controller.write_serial(command) != len(command):
            self.expected = None
            self.message.SetLabel('Write failed; read settings before retrying.')
            self.save.Disable()
            return
        self.read()

    def on_timeout(self, event):
        self.timer.Stop()
        self.expected = None
        self.save.Disable()
        self.refresh.Enable(self.controller.connected)
        self.message.SetLabel('Settings read-back failed or timed out; values were not assumed.')
        print('Scan/search settings read-back failed or timed out', file=sys.stderr)

    def on_close(self, event):
        self.timer.Stop()
        self.controller.settings_dialog = None
        self.controller.update_connection_ui()
        self.Destroy()


class AorCtrl(AorCtrlFrame):
    """Simple terminal program for wxPython"""

    def __init__(self, *args, **kwds):
        self.serial = serial.Serial(baudrate=19200, bytesize=8, stopbits=2,
                                    parity='N', rtscts=0, xonxoff=1)

        self.serial.port = 'COM14'     # if serial is instantiated with port parameter, then it is opened
        self.serial.timeout = 1        # make sure that the alive event can be checked from time to time
        self.thread = None
        self.row = 0
        self.last = None
        self.vfa = None
        self.vfb = None
        self.vfo = None
        self.vfo_status = {}
        self.background_vfo = None
        self.memory_banks = {}
        self.last_memory_bank = None
        self.initial_bank_load_pending = False
        self.level_squelch_value = None
        self.level_squelch_pending = False
        self.level_squelch_requested = None
        self.receiver_switches = {'NL': None, 'AF': None}
        self.receiver_switch_pending = set()
        self.afc_mode = None
        self.search_banks = {}
        self.search_bank_rows = {}
        self.search_bank_dialog = None
        self.edit_bank_pending = None
        self.bank_write_pending = None
        self.groups = {'scan': {}, 'search': {}}
        self.group_loading = {'scan': None, 'search': None}
        self.group_response = {'scan': None, 'search': None}
        self.group_dialogs = {'scan': None, 'search': None}
        self.search_restore_command = None
        self.active_operation = None
        self.connected = False
        self.filling_banks = False
        self.pending_memory_channels = set()
        self.memory_rows = {}
        self.memory_empty_channels = set()
        self.memory_channel_dialog = None
        self.memory_channel_pending = None
        self.memory_inventory = None
        self.memory_select_read = None
        self.memory_flag_edit = None
        self.memory_flag_cells_pending = False
        self.monitor_muted = False
        self.settings_dialog = None
        self.receiver_status = None
        self.receiving_channel = None
        self.vfo_channel = None
        self.operation_signal_open = False
        self.alive = threading.Event()
        self.lm_pending = False
        self.lc_enabled = False
        self.signal_level = None
        self.squelch_open = None
        self.activity_events = deque(maxlen=200)
        self.active_transmission = None
        self.select_scan_rows = {}
        self.pass_frequency_rows = {}
        self.pass_context = 'V'
        self.bandscope = None
        AorCtrlFrame.__init__(self, *args, **kwds)
        self.edit_list.create_memory_view(self)
        self.bt_newchannel = wx.Button(self.panel_1, label='New Channel...')
        self.bt_mkpassfreq.GetContainingSizer().Add(self.bt_newchannel, 0, wx.EXPAND | wx.ALL, 3)
        self.bt_newchannel.SetToolTip('Create a channel in an unused location of the displayed Memory Bank.')

        self.now_panel = wx.Panel(self)
        now_sizer = wx.StaticBoxSizer(wx.VERTICAL, self.now_panel, 'Now Receiving')
        self.now_text = wx.StaticText(self.now_panel, label='—')
        now_sizer.Add(self.now_text, 0, wx.EXPAND | wx.ALL, 4)
        self.now_panel.SetSizer(now_sizer)
        self.GetSizer().Insert(1, self.now_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 5)
        self.bt_settings = wx.Button(self.operation_panel, label='Settings...')
        # Keep the existing common operation buttons in one compact row.
        self.bt_start.GetContainingSizer().Add(self.bt_settings, 0, wx.LEFT, 5)
        self.bt_settings.SetToolTip('Read and edit timing and signal acceptance settings for this operation/context.')

        self.create_monitor_controls()
        self.lm_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_lm_timer, self.lm_timer)
        self.memory_flag_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_memory_flag_timeout, self.memory_flag_timer)
        self.memory_channel_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_memory_channel_timeout, self.memory_channel_timer)
        self.inventory_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda event: self.finish_memory_inventory('Export failed: scanner read timed out.'), self.inventory_timer)
        self.level_squelch_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_level_squelch_timer, self.level_squelch_timer)
        self.__set_properties()
        self.__attach_events()           # register events
        view_menu = wx.Menu()
        bandscope_action = view_menu.Append(wx.ID_ANY, 'BAND SCOPE...')
        self.GetMenuBar().Append(view_menu, 'View')
        self.Bind(wx.EVT_MENU, self.on_bandscope, id=bandscope_action.GetId())
        bandscope_action.SetHelp('Open the bandscope viewer; connect to read scanner data.')
        tools_menu = wx.Menu()
        self.inventory_action = tools_menu.Append(wx.ID_ANY, 'Export Memory Inventory...')
        self.GetMenuBar().Append(tools_menu, 'Tools')
        self.Bind(wx.EVT_MENU, self.on_export_memory_inventory, id=self.inventory_action.GetId())
        self.setup_usability()

    def setup_usability(self):
        toolbar = self.GetToolBar()
        tools = [toolbar.GetToolByPos(index) for index in range(toolbar.GetToolsCount())]
        for tool in tools:
            name = tool.GetLabel()
            if name == 'connect':
                self.connect_tool_id = tool.GetId()
            elif name == 'config':
                self.config_tool_id = tool.GetId()
                tool.SetLabel('Serial Config')
                toolbar.SetToolShortHelp(tool.GetId(),
                                        'Configure the serial port and baud rate (disconnects first).')
            else:
                label, tip = {
                    'newlog': ('New Log', 'Create a log file (not implemented).'),
                    'open': ('Open', 'Open a log file (not implemented).'),
                    'upload': ('Upload', 'Upload data (not implemented).'),
                }[name]
                if name in ('newlog', 'open'):
                    item = self.mfile.Append(tool.GetId(), label, tip)
                    item.Enable(False)
                    self.Bind(wx.EVT_MENU, self.log_new if name == 'newlog' else self.log_open,
                              id=tool.GetId())
                    toolbar.DeleteTool(tool.GetId())
                else:
                    tool.SetLabel(label)
                    toolbar.SetToolShortHelp(tool.GetId(), tip)
                    toolbar.EnableTool(tool.GetId(), False)
        toolbar.Realize()
        edit_index = self.GetMenuBar().FindMenu('Edit')
        if edit_index != wx.NOT_FOUND and self.GetMenuBar().GetMenu(edit_index).GetMenuItemCount() == 0:
            self.GetMenuBar().Remove(edit_index).Destroy()
        for label in (self.lb_vfa, self.lb_vfb, self.lb_vfo):
            label.SetLabel('----.-----')

        self.aor_status.SetFieldsCount(2)
        self.aor_status.SetStatusWidths([-1, 260])
        self.aor_status.SetStatusText('Choose Serial Config, then Connect.')
        for control, tip in (
                (self.rb_vfos, 'Select the active receiver VFO.'),
                (self.cbx_mode, 'Set the receiver modulation mode.'),
                (self.cbx_step, 'Set the receiver tuning step in kHz.'),
                (self.ckbx_auto, 'Enable automatic receiver settings for the tuned frequency.'),
                (self.ckbx_att, 'Enable or disable the receiver attenuator.'),
                (self.cbx_lists, 'Choose memory, search, Select Scan, pass-frequency or local log data.'),
                (self.bt_refresh, 'Reload the selected list; for pass frequencies, choose a bank or VFO.'),
                (self.operation_choice, 'Choose memory scanning or frequency searching.'),
                (self.source_choice, 'Choose the Memory Bank, linked group, selected-channel list, or frequency range to use.'),
                (self.memory_bank_choice, 'Memory Bank: a stored collection of individual memory channels. Select the bank to scan.'),
                (self.search_bank_choice, 'Search Bank: a stored frequency range with its search parameters. Read the selected bank.'),
                (self.bt_start, 'Start the selected operation and source using its scanner commands.'),
                (self.bt_mkpassfreq, 'Add a frequency to the selected AR8600 pass-frequency list.'),
                (self.edit_list.list, 'View the selected list; right-click a pass frequency or stored range for actions.'),
                (self.activity_list, 'View the most recent receiver activity; LOG VIEW shows the full in-memory log.'),
                (self.signal_gauge, 'Raw AR8600 signal level (0-255), without S-unit or dBm calibration.'),
                (self.signal_text, 'Raw AR8600 signal level (0-255).'),
                (self.squelch_text, 'OPEN means reception is audible; CLOSED means the squelch is muting it.'),
                (self.tuning_panel.tune_freq, 'Enter a frequency in MHz; Enter applies it to the receiver.'),
                (self.tuning_panel.tune_benter, 'Set the receiver frequency to the entered MHz value.'),
                (self.tuning_panel.tune_back, 'Delete the last character of the frequency entry.'),
                (self.tuning_panel.tune_bdot, 'Append a decimal point to the frequency entry.'),
                (self.tuning_panel.tune_rev, 'Tune down using the current receiver step.'),
                (self.tuning_panel.tune_forw, 'Tune up using the current receiver step.'),
                (self.tuning_panel.tune_frev, 'Tune down using the receiver fast tuning control.'),
                (self.tuning_panel.tune_ffor, 'Tune up using the receiver fast tuning control.')):
            control.SetToolTip(tip)
        for digit in range(10):
            getattr(self.tuning_panel, 'tune_b%d' % digit).SetToolTip(
                'Append %d to the frequency entry in MHz.' % digit)
        for control, tip in (
                (self.ckbx_nl, 'Noise limiter; effective in AM and SSB modes.'),
                (self.ckbx_afc, 'Automatic frequency control; available in NFM, SFM, WAM, AM and NAM.'),
                ):
            control.SetToolTip(tip)
        for control, tip in (
                (self.cbx_lists, 'Read stored channels, stored ranges (Search Banks), Selected Channels (Select Scan), pass frequencies or local logs.'),
                (self.cbx_scan, 'Scan Group: Memory Banks linked together for memory scanning. Read group 0-9; 0 is fixed LINK OFF.'),
                (self.bt_scgrp, 'View linked Memory Banks; edit membership for Scan Groups 1-9.'),
                (self.cbx_search, 'Search Group: Search Banks linked together for frequency searching. Read group 0-9; 0 is fixed LINK OFF.'),
                (self.bt_search, 'View linked stored frequency ranges; edit Search Groups 1-9.'),
                (self.range_lower, 'Lower frequency limit in MHz; configures VFO-A, without writing a Search Bank.'),
                (self.range_upper, 'Upper frequency limit in MHz; must exceed Lower; configures VFO-B.'),
                (self.range_step, 'Search step in kHz. Auto uses the scanner bandplan and may override this step.'),
                (self.range_mode, 'Choose modulation, or Auto for the scanner bandplan mode and step.'),
                (self.bt_stop, 'Stop the active scan/search, restore normal receiver operation, and request RX.'),
                (self.bt_newsearchbank, 'Create a stored Search Bank: lower/upper range and search parameters.'),
                (self.bt_editsearchbank, 'Read and edit the selected stored Search Bank; retain its attenuation.'),
                ):
            control.SetToolTip(tip)
        self.update_connection_ui()
        self.sql.SetToolTip('Signal-level squelch threshold for VFO and frequency-range searching; 0 disables it.')
        self.sql_value.SetToolTip('Scanner-confirmed level squelch threshold; Off means DB000.')

    def update_connection_ui(self):
        port_open = self.serial.is_open and self.alive.is_set()
        ready = port_open and self.connected
        self.update_receiver_switch_ui()
        state = 'Connected' if ready else 'Connecting'
        self.aor_status.SetStatusText(
            '%s: %s @ %s baud' % (state, self.serial.port, self.serial.baudrate)
            if port_open else 'Disconnected', 1)
        toolbar = self.GetToolBar()
        toolbar.FindById(self.connect_tool_id).SetLabel(
            'Disconnect' if self.serial.is_open else 'Connect')
        toolbar.SetToolShortHelp(self.connect_tool_id,
                                'Disconnect from the AR8600.' if self.serial.is_open else
                                'Connect to the AR8600 using the configured serial port.')
        for control in (self.rb_vfos, self.cbx_mode, self.cbx_step, self.ckbx_auto,
                        self.ckbx_att,
                        self.cbx_scan, self.bt_scgrp, self.bt_stop, self.cbx_search, self.bt_search,
                        self.range_lower, self.range_upper, self.range_step, self.range_mode,
                        self.bt_newsearchbank, self.bt_editsearchbank,
                        self.tuning_panel.tune_benter, self.tuning_panel.tune_frev,
                        self.tuning_panel.tune_rev, self.tuning_panel.tune_forw,
                        self.tuning_panel.tune_ffor):
            control.Enable(ready and self.memory_inventory is None)
        self.cbx_lists.Enable(self.memory_channel_pending is None and self.memory_inventory is None)
        self.operation_choice.Enable()
        self.source_choice.Enable()
        self.sql.Enable(ready and self.memory_inventory is None and self.level_squelch_value is not None and not self.level_squelch_pending)
        self.bt_settings.Enable(ready and self.active_operation is None)
        if not port_open:
            self.receiver_status = None
            self.vfo_channel = None
            self.show_now_receiving()
        pending = self.memory_flag_edit is not None or self.memory_channel_pending is not None or self.memory_inventory is not None
        if pending != self.memory_flag_cells_pending:
            self.memory_flag_cells_pending = pending
            # Tell the model that only the flag cells' editability changed.
            for row in range(len(self.edit_list.memory_model.channels)):
                self.edit_list.memory_model.RowChanged(row)
        view = self.current_list_view()
        self.bt_refresh.Enable((ready and (bool(view) or self.selected_memory_bank() is not None) and view != 'DATABASE') or view == 'LOG VIEW')
        if self.memory_inventory is not None and view != 'LOG VIEW':
            self.bt_refresh.Disable()
        self.bt_start.Enable(ready)
        self.update_operation_ui()
        self.memory_bank_choice.Enable(self.memory_channel_pending is None and self.memory_inventory is None)
        self.inventory_action.Enable(self.memory_inventory_available())
        self.bt_newchannel.Show(self.selected_memory_bank() is not None)
        self.bt_newchannel.Enable(self.memory_channel_available() and bool(self.memory_empty_channels))
        self.bt_mkpassfreq.Enable(ready and view == 'PASS FREQS')
        self.bt_mkpassfreq.Show(view == 'PASS FREQS')
        self.panel_1.Layout()
        if self.bandscope is not None:
            self.bandscope.set_controls_enabled(self.bandscope.waiting is None)

    def on_bandscope(self, event):
        if self.memory_flag_edit is not None or self.memory_channel_pending is not None or self.memory_inventory is not None:
            self.aor_status.SetStatusText('Wait for the memory-channel flag read-back before opening bandscope.')
            return
        if self.bandscope is None:
            self.bandscope = BandscopeWindow(self)
        self.bandscope.close_requested = False
        self.bandscope.Show()
        self.bandscope.Raise()
        if self.bandscope.waiting is None and self.bandscope.available():
            self.bandscope.request_status()

    def update_receiver_switch_ui(self):
        ready = self.connected and self.serial.is_open and self.alive.is_set() and self.memory_inventory is None
        self.ckbx_nl.Enable(ready and self.receiver_switches['NL'] is not None and 'NL' not in self.receiver_switch_pending)
        self.ckbx_afc.Enable(ready and self.afc_mode in (1, 2, 6, 7, 8) and
                             self.receiver_switches['AF'] is not None and 'AF' not in self.receiver_switch_pending)

    def on_receiver_switch(self, event):
        control = event.GetEventObject()
        key = 'NL' if control is self.ckbx_nl else 'AF'
        desired = control.GetValue()
        control.SetValue(bool(self.receiver_switches[key]))
        if not control.IsEnabled():
            return
        self.receiver_switch_pending.add(key)
        self.update_receiver_switch_ui()
        command = ('%s%d\r\n%s\r\n' % (key, desired, key)).encode('ascii')
        if self.write_serial(command) != len(command):
            self.receiver_switch_pending.discard(key)
            self.aor_status.SetStatusText('%s write failed; state was not verified.' % key)
            self.update_receiver_switch_ui()

    def set_receiver_switch(self, text):
        if re.fullmatch(r'(NL|AF)[01]', text) is None:
            raise ValueError('Invalid noise limiter / AFC response')
        key = text[:2]
        value = text[2] == '1'
        self.receiver_switches[key] = value
        self.receiver_switch_pending.discard(key)
        (self.ckbx_nl if key == 'NL' else self.ckbx_afc).SetValue(value)
        self.update_receiver_switch_ui()

    def operation_selection(self):
        operation = self.operation_choice.GetSelection()
        source = self.source_choice.GetSelection()
        return (('Memory Scan', ('Current Bank', 'Scan Group', 'Select Scan')[source]) if operation == 0 else
                ('Frequency Search', ('Range', 'Search Bank', 'Search Group')[source]))

    def update_operation_ui(self):
        operation, source = self.operation_selection()
        ready = self.connected and self.serial.is_open and self.alive.is_set() and self.memory_inventory is None
        tips = {'Current Bank': 'Memory Bank: a stored collection of individual memory channels. Scan only the selected bank.',
                'Scan Group': 'AR8600 Scan Group: several Memory Banks linked for scanning.',
                'Select Scan': 'AR8600 Select Scan: only memory channels explicitly selected; memory PASS flags are ignored.',
                'Range': 'Search between two VFO frequency limits without creating a stored Search Bank.',
                'Search Bank': 'AR8600 Search Bank: a saved frequency-search range.',
                'Search Group': 'AR8600 Search Group: several Search Banks linked together.'}
        self.source_choice.SetToolTip(tips[source])
        idle = self.active_operation is None
        panels = (self.memory_source_panel, self.scan_source_panel, self.range_source_panel,
                  self.bank_source_panel, self.search_source_panel)
        selected = {('Memory Scan', 'Current Bank'): self.memory_source_panel,
                    ('Memory Scan', 'Scan Group'): self.scan_source_panel,
                    ('Frequency Search', 'Range'): self.range_source_panel,
                    ('Frequency Search', 'Search Bank'): self.bank_source_panel,
                    ('Frequency Search', 'Search Group'): self.search_source_panel}.get((operation, source))
        for panel in panels:
            panel.Show(panel is selected)
            panel.Enable(ready and idle and panel is selected)
        for kind, control in (('scan', self.cbx_scan), ('search', self.cbx_search)):
            panel = self.scan_source_panel if kind == 'scan' else self.search_source_panel
            control.Enable(ready and idle and panel is selected and self.group_loading[kind] is None)
        self.bt_start.Enable(ready and idle)
        self.bt_stop.Enable(ready)
        self.bt_settings.Enable(ready and idle)
        if self.settings_dialog is not None:
            self.bt_start.Disable()
            self.operation_choice.Disable()
            self.source_choice.Disable()
            for panel in panels:
                panel.Disable()
        self.operation_panel.Layout()
        self.panel_1.Layout()
        self.Layout()

    def on_operation_changed(self, event):
        choices = ['Current Memory Bank', 'Linked Memory Banks', 'Selected Channels'] if self.operation_choice.GetSelection() == 0 else ['Manual Range', 'Stored Range', 'Linked Ranges']
        self.source_choice.SetItems(choices)
        self.source_choice.SetSelection(0)
        self.on_source_changed(event)

    def on_source_changed(self, event):
        self.update_operation_ui()
        if not self.connected or not self.serial.is_open or self.active_operation is not None:
            return
        operation, source = self.operation_selection()
        if source == 'Scan Group':
            self.request_group('scan')
        elif source == 'Search Group':
            self.request_group('search')
        elif source == 'Search Bank':
            self.select_list_view('SEARCH BANKS')
            self.on_select_list(None)
        elif source == 'Select Scan':
            self.select_list_view('SELECT SCAN')
            self.on_select_list(None)

    def operation_memory_bank(self):
        index = self.memory_bank_choice.GetSelection()
        return self.memory_bank_choice.GetClientData(index) if index != wx.NOT_FOUND else self.selected_memory_bank()

    def sync_memory_choices(self, selected=None):
        previous = selected or self.operation_memory_bank()
        self.memory_bank_choice.Clear()
        for bank in sorted(self.memory_banks, key=lambda b: (b.upper(), b.islower())):
            metadata = self.memory_banks[bank]
            name = metadata['name']
            label = name if name and name != bank else '%s:%d' % (bank, metadata['channels'])
            index = self.memory_bank_choice.Append(label, metadata)
            if previous and previous['bank'] == bank:
                self.memory_bank_choice.SetSelection(index)

    def on_memory_source_changed(self, event):
        metadata = self.operation_memory_bank()
        if metadata is None:
            return
        for index in range(self.cbx_lists.GetCount()):
            data = self.cbx_lists.GetClientData(index)
            if data and data['bank'] == metadata['bank']:
                self.cbx_lists.SetSelection(index)
                self.on_select_list(None)
                break

    def on_search_source_changed(self, event):
        if not self.connected or not self.serial.is_open:
            return
        if self.current_list_view() != 'SEARCH BANKS':
            self.select_list_view('SEARCH BANKS')
            self.search_bank_rows.clear()
            self.filling_banks = False
            self.pending_memory_channels.clear()
            self.prepare_list((('Stored Range', 85), ('Lower', 125), ('Upper', 125), ('Step', 100),
                               ('Auto', 45), ('Mode', 55), ('ATT', 40), ('Name', 130)))
        bank = self.search_bank_choice.GetClientData(self.search_bank_choice.GetSelection())
        self.write_serial(('SR%s\r\n' % bank).encode('ascii'))
        self.update_connection_ui()

    def on_operation_start(self, event):
        if not self.connected or not self.serial.is_open or self.active_operation is not None or self.memory_channel_pending is not None:
            return
        operation, source = self.operation_selection()
        started = False
        if operation == 'Memory Scan':
            if source == 'Current Bank':
                metadata = self.operation_memory_bank()
                if metadata is None:
                    self.aor_status.SetStatusText('Choose a Memory Bank to scan.')
                    return
                command = ('GM0\r\nMS%s\r\nRX\r\n' % metadata['bank']).encode('ascii')
                started = self.write_serial(command) == len(command)
            else:
                started = self.on_select_scan_start(event, source=source)
        elif source == 'Range':
            started = self.on_vfo_start(event)
        elif source == 'Search Bank':
            bank = self.search_bank_choice.GetClientData(self.search_bank_choice.GetSelection())
            started = self.on_start_search_bank(event, bank=bank)
        else:
            started = self.on_group_search(event)
        if started:
            self.active_operation = (operation, source)
            self.operation_signal_open = False
            self.show_now_receiving()
            self.aor_status.SetStatusText('%s started: %s' % (operation, source))
            self.update_operation_ui()

    def on_operation_stop(self, event):
        if not self.connected or not self.serial.is_open:
            return
        # Range Search has cached VFO settings to restore; other sources leave
        # scanning/searching by selecting normal receiver mode, then reading RX.
        if self.active_operation == ('Frequency Search', 'Range') or self.search_restore_command is not None:
            self.on_vfo_stop(event)
        else:
            self.write_serial(self.normal_vfo_command() + b'RX\r\n')
        self.active_operation = None
        self.operation_signal_open = False
        self.receiver_status = None
        self.show_now_receiving()
        self.update_operation_ui()

    def open_operation_settings(self, event):
        if self.settings_dialog is not None:
            self.settings_dialog.Raise()
            return
        if not self.connected or self.memory_flag_edit is not None or self.memory_channel_pending is not None or self.active_operation is not None:
            return
        operation, source = self.operation_selection()
        family = 'X' if operation == 'Memory Scan' else 'D' if source == 'Range' else 'S'
        group = (0 if source == 'Current Bank' else int(self.cbx_scan.GetValue()) if source == 'Scan Group' else
                 int(self.cbx_search.GetValue()) if source == 'Search Group' else None)
        self.settings_dialog = OperationSettingsDialog(self, family, group)
        self.settings_dialog.Show()
        self.settings_dialog.read()
        self.update_connection_ui()

    def accept_operation_parameter(self, text):
        key, value = parse_operation_parameter(text)
        if key == 'DB':
            self.level_squelch_value = value
            self.level_squelch_pending = False
            self.sql.SetValue(value)
            self.sql_value.SetLabel('Off' if value == 0 else str(value))
            self.update_connection_ui()
        if self.settings_dialog is not None:
            self.settings_dialog.accept(key, value)

    def show_now_receiving(self):
        previous = self.receiving_channel
        self.receiving_channel = None
        moving = self.active_operation is not None and not self.operation_signal_open
        state = self.receiver_status
        if not self.connected or not self.serial.is_open or state is None and not moving:
            text = '—'
        elif moving:
            text = 'Scanning...' if self.active_operation[0] == 'Memory Scan' else 'Searching...'
        else:
            channel = state.get('channel')
            name = state.get('name', '')
            identity = self.vfo_channel
            if (not channel and identity is not None and identity['confirmed'] and
                    all(state.get(key) is None or state[key] == value
                        for key, value in identity['expected'].items())):
                channel, name = identity['channel'], identity['name']
            if channel:
                text = channel + (' — ' + name if name else '')
                self.receiving_channel = channel
            else:
                text = '—'
        self.now_text.SetLabel(text)
        self.now_text.SetForegroundColour(wx.SystemSettings.GetColour(
            wx.SYS_COLOUR_GRAYTEXT if moving else wx.SYS_COLOUR_WINDOWTEXT))
        for channel in (previous, self.receiving_channel):
            if channel in self.memory_rows:
                self.show_memory_flags(channel)

    def clear_vfo_channel(self):
        """A manual receiver change ends the explicit memory-to-VFO association."""
        self.vfo_channel = None
        self.show_now_receiving()

    def receive_context_status(self, text):
        """Only called for validated active RX statuses, never cached/background VFOs."""
        if self.monitor_muted:
            return
        memory = text.startswith(('MR ', 'MS ', 'SM '))
        fields = text.split(None, 8)[1:] if memory else text.split(None, 6)[1:] if re.match(r'SR[A-Ta-t] RF', text) else text.split()[1:]
        if text.startswith(('VS ', 'VV ')):
            fields = fields[1:]
        values = {field[:2]: field[2:] for field in fields}
        state = {'context': {'VA': 'VFO-A', 'VB': 'VFO-B', 'VF': 'VFO'}.get(text[:2], text.split()[0]),
                 'frequency_hz': protocol_frequency_hz(values['RF']),
                 'step_hz': protocol_step_hz(values['ST']), 'mode': int(values['MD']),
                 'auto': int(values['AU']), 'attenuator': int(values['AT']),
                 'channel': values.get('MX'), 'name': values.get('TM', values.get('TT', ''))}
        identity = self.vfo_channel
        if identity is not None:
            if any(state[key] != value for key, value in identity['expected'].items()):
                self.vfo_channel = None
            else:
                # With Auto on, the first RX supplies the scanner's chosen mode/step.
                identity['expected'].update(mode=state['mode'], step_hz=state['step_hz'])
                identity['confirmed'] = True
        if (self.active_operation is not None and self.operation_signal_open and
                self.receiver_status is not None and
                (state['channel'], state['frequency_hz']) !=
                (self.receiver_status.get('channel'), self.receiver_status['frequency_hz'])):
            # A moving RX cursor is not a new squelch-open/paused-channel event.
            self.operation_signal_open = False
        self.receiver_status = state
        self.afc_mode = state['mode']
        self.update_receiver_switch_ui()
        if text.startswith(('MS ', 'SM ', 'VS ', 'VV ')) or re.match(r'SR[A-Ta-t] RF', text):
            if self.active_operation is None:
                self.active_operation = ('Memory Scan', 'Current Bank') if memory else ('Frequency Search', 'Search Bank')
            # An RX scan cursor alone does not prove that reception is paused.
        elif text.startswith(('VA ', 'VB ', 'VF ', 'MR ')):
            self.active_operation = None
            self.operation_signal_open = False
        self.show_now_receiving()

    def receive_activity_context(self, activity):
        if self.monitor_muted:
            return
        if self.active_operation is not None and self.active_operation[0] == 'Memory Scan' and activity['source'] != 'Memory':
            return  # Ignore a preceding VFO's queued LC event while scan starts.
        was_open = self.operation_signal_open
        if self.active_operation is not None:
            self.operation_signal_open = activity['squelch_open']
        if activity['squelch_open']:
            channel = activity['source_id'] if activity['source'] == 'Memory' else None
            context = ('VFO-' + activity['source_id'] if activity['source'] == 'VFO' and activity['source_id'] in ('A', 'B') else
                       'VFO' if activity['source'] == 'VFO' else 'Stored Range ' + activity['source_id'])
            cached = self.memory_rows.get(channel)
            fields = cached['fields'] if cached else None
            state = {'context': context, 'channel': channel, 'frequency_hz': activity['frequency_hz'],
                     'name': fields[7] if fields else '', 'mode': None, 'step_hz': None}
            if fields and protocol_frequency_hz(fields[2]) == activity['frequency_hz']:
                state.update(mode=int(fields[5]), step_hz=protocol_step_hz(fields[3]))
            elif self.receiver_status is not None and (self.receiver_status['context'], self.receiver_status['frequency_hz']) == (context, activity['frequency_hz']):
                state.update(mode=self.receiver_status['mode'], step_hz=self.receiver_status['step_hz'])
            self.receiver_status = state
            if state['mode'] is None or channel and not was_open and self.active_operation is not None:
                # One RX on an LC1 open supplies details absent from LC; no new polling.
                self.write_serial(b'RX\r\n')
        self.show_now_receiving()

    def on_memory_activate(self, event):
        if not event.GetItem().IsOk() or event.GetColumn() in (1, 2):
            return
        channel = self.edit_list.memory_model.channels[self.edit_list.memory_model.GetRow(event.GetItem())]
        self.tune_memory_channel(channel)

    def on_memory_double_click(self, event):
        item, column = self.edit_list.memory.HitTest(event.GetPosition())
        if not item.IsOk() or column is None or column.GetModelColumn() in (0, 1, 2):
            event.Skip()  # Channel uses native activation; toggle columns keep their own behavior.
            return
        # Windows native item activation is limited to the primary column.
        channel = self.edit_list.memory_model.channels[self.edit_list.memory_model.GetRow(item)]
        self.tune_memory_channel(channel)

    def tune_memory_channel(self, channel):
        if (not self.connected or not self.serial.is_open or not self.alive.is_set() or
                self.filling_banks or self.memory_select_read is not None or self.memory_flag_edit is not None or
                self.memory_channel_pending is not None or
                self.monitor_muted or self.active_operation is not None or self.settings_dialog is not None or
                self.background_vfo is not None or
                self.bandscope is not None and (self.bandscope.entered or self.bandscope.waiting is not None)):
            return
        fields = self.memory_rows[channel]['fields']
        frequency, step, auto, mode, att = fields[2:7]
        context = self.normal_vfo_command()
        self.clear_vfo_channel()
        expected = {'context': {b'VA\r\n': 'VFO-A', b'VB\r\n': 'VFO-B', b'VF\r\n': 'VFO'}[context],
                    'frequency_hz': protocol_frequency_hz(frequency), 'auto': int(auto), 'attenuator': int(att)}
        if auto == '0':
            expected.update(mode=int(mode), step_hz=protocol_step_hz(step))
        self.vfo_channel = {'channel': channel, 'name': fields[7], 'expected': expected, 'confirmed': False}
        command = context + ('RF%s\r\nAT%s\r\n' % (frequency, att)).encode('ascii')
        command += (b'AU1\r\n' if auto == '1' else ('AU0\r\nMD%s\r\nST%s\r\n' % (mode, step)).encode('ascii'))
        self.monitor_muted = True
        try:
            if self.write_serial(b'MC1\r\n' + command) != len(command) + 5:
                self.vfo_channel = None
                self.aor_status.SetStatusText('Channel tuning write failed; requesting actual receiver state.')
        finally:
            self.release_monitor_mute()
            if self.serial.is_open:
                self.write_serial(b'RX\r\n')

    def release_monitor_mute(self):
        if self.monitor_muted and self.serial.is_open:
            if self.write_serial(b'MC0\r\n') != 5:
                print('Could not release temporary monitor mute (MC0)', file=sys.stderr)
                self.aor_status.SetStatusText('MC0 failed: temporary squelch mute could not be released. See serial diagnostics.')
                return False
        self.monitor_muted = False
        return True

    def start_thread(self):
        """Start the receiver thread"""
        self.thread = threading.Thread(target=self.com_thread)
        self.thread.daemon = True
        self.alive.set()
        self.thread.start()

    def maybe_load_initial_bank(self):
        if (not self.initial_bank_load_pending or not self.connected or self.background_vfo is not None or
                len(self.memory_banks) != 20):
            return
        self.initial_bank_load_pending = False
        bank = self.last_memory_bank
        if bank not in self.memory_banks or not self.memory_banks[bank]['channels']:
            named = [entry for identifier, entry in sorted(self.memory_banks.items(), key=lambda pair: (pair[0].upper(), pair[0].islower()))
                     if entry['channels'] and entry['name'].strip() and entry['name'].strip() != identifier]
            preferred = next((entry for entry in named if entry['name'].strip().casefold() == 'fm radio'), None)
            bank = (preferred or (named[0] if named else {})).get('bank')
        if bank is None:
            return
        for index in range(5, self.cbx_lists.GetCount()):
            if self.cbx_lists.GetClientData(index)['bank'] == bank:
                self.cbx_lists.SetSelection(index)
                self.on_select_list(None)
                break

    def on_level_squelch_change(self, event):
        if not self.connected or not self.serial.is_open or self.level_squelch_pending:
            return
        self.level_squelch_requested = self.sql.GetValue()
        self.level_squelch_timer.StartOnce(250)

    def on_level_squelch_timer(self, event):
        value = self.level_squelch_requested
        self.level_squelch_requested = None
        if value is None or not self.connected or not self.serial.is_open or value == self.level_squelch_value:
            return
        self.level_squelch_pending = True
        self.sql.SetValue(self.level_squelch_value)
        self.update_connection_ui()
        command = ('DB%03d\r\nDB\r\n' % value).encode('ascii')
        if self.write_serial(command) != len(command):
            self.level_squelch_pending = False
            self.aor_status.SetStatusText('Level Squelch write failed; value was not verified.')
            self.update_connection_ui()

    def stop_thread(self):
        """Stop the receiver thread, wait util it's finished."""
        if self.memory_inventory is not None:
            self.finish_memory_inventory('Export cancelled: receiver disconnected.')
        if self.memory_channel_pending is not None:
            self.finish_memory_channel_edit('Channel operation interrupted; read-back was not completed.')
        if self.memory_channel_dialog is not None:
            self.memory_channel_dialog.Close()
        if self.memory_flag_edit is not None and self.memory_flag_edit['restore'] is not None and not self.memory_flag_edit.get('restored') and self.serial.is_open:
            self.write_serial(self.memory_flag_edit['restore'])
        self.release_monitor_mute()
        if self.settings_dialog is not None:
            self.settings_dialog.Close()
        if self.bandscope is not None:
            self.bandscope.disconnect()
        self.stop_monitoring()
        self.connected = False
        self.active_operation = None
        self.filling_banks = False
        self.pending_memory_channels.clear()
        self.initial_bank_load_pending = False
        self.level_squelch_timer.Stop()
        self.level_squelch_pending = False
        self.level_squelch_requested = None
        self.level_squelch_value = None
        self.receiver_switches = {'NL': None, 'AF': None}
        self.receiver_switch_pending.clear()
        self.afc_mode = None
        self.ckbx_nl.SetValue(False)
        self.ckbx_afc.SetValue(False)
        self.sql_value.SetLabel('---')
        self.memory_flag_timer.Stop()
        self.memory_select_read = None
        self.memory_flag_edit = None
        self.background_vfo = None
        if self.thread is not None:
            self.alive.clear()          # clear alive event for thread
            cancel_read = getattr(self.serial, 'cancel_read', None)
            if self.serial.is_open and callable(cancel_read):
                try:
                    cancel_read()
                except (serial.SerialException, OSError, NotImplementedError) as error:
                    print('Serial read cancellation error: %s' % error, file=sys.stderr)
            self.thread.join()          # wait until thread has finished
            self.thread = None
        self.update_connection_ui()

    def create_monitor_controls(self):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        meter = wx.BoxSizer(wx.HORIZONTAL)
        self.signal_text = wx.StaticText(panel, label='Signal: --- / 255', size=(115, -1))
        self.signal_gauge = wx.Gauge(panel, range=255, style=wx.GA_HORIZONTAL)
        self.squelch_text = wx.StaticText(panel, label='Squelch: ---', size=(145, -1))
        meter.Add(self.signal_text, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        meter.Add(self.signal_gauge, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        meter.Add(self.squelch_text, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(meter, 0, wx.EXPAND | wx.ALL, 5)
        sizer.Add(wx.StaticText(panel, label='Recent activity'), 0, wx.LEFT | wx.BOTTOM, 5)
        self.activity_list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL,
                                         size=(-1, 130))
        for index, (label, width) in enumerate((('Time', 155), ('Frequency', 130),
                                               ('Source', 100), ('Level', 55),
                                               ('Duration', 80), ('Squelch', 85))):
            self.activity_list.InsertColumn(index, label, width=width)
        sizer.Add(self.activity_list, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        panel.SetSizer(sizer)
        self.GetSizer().Add(panel, 0, wx.EXPAND)
        self.GetSizer().Fit(self)
        self.Layout()

    def start_monitoring(self):
        if self.lm_timer.IsRunning() or not self.connected or not self.serial.is_open:
            return
        self.lm_pending = False
        self.level_squelch_pending = True
        self.write_serial(b'DB\r\n')
        self.receiver_switch_pending.update(('NL', 'AF'))
        if self.write_serial(b'NL\r\nAF\r\n') != 8:
            self.receiver_switch_pending.clear()
        if self.write_serial(b'LC1\r\n') == 5:
            self.lc_enabled = True
            self.lm_timer.Start(250)

    def stop_monitoring(self):
        self.lm_timer.Stop()
        self.lm_pending = False
        enabled = self.lc_enabled
        self.lc_enabled = False
        if enabled and self.serial.is_open:
            self.write_serial(b'LC0\r\n')
        self.active_transmission = None
        self.signal_level = self.squelch_open = None
        self.signal_gauge.SetValue(0)
        self.signal_text.SetLabel('Signal: --- / 255')
        self.squelch_text.SetLabel('Squelch: ---')

    def on_lm_timer(self, event):
        if not self.connected or not self.serial.is_open or not self.alive.is_set():
            self.stop_monitoring()
            self.update_connection_ui()
            return
        if self.lm_pending:
            return
        if self.monitor_muted:
            return
        if self.bandscope is not None and self.bandscope.pauses_lm:
            return
        self.lm_pending = True
        if self.write_serial(b'LM\r\n') != 4:
            self.stop_monitoring()

    def update_signal(self, level, squelch_open):
        self.signal_level, self.squelch_open = level, squelch_open
        self.signal_gauge.SetValue(level)
        self.signal_text.SetLabel('Signal: %d / 255' % level)
        self.squelch_text.SetLabel('Squelch: %s' % ('OPEN' if squelch_open else 'CLOSED'))
        if self.active_operation is not None and not squelch_open and not self.monitor_muted:
            self.operation_signal_open = False
            self.show_now_receiving()

    def set_signal_level(self, text):
        self.lm_pending = False
        level, squelch_open = parse_lm_response(text)
        self.update_signal(level, squelch_open)

    def record_activity(self, text):
        activity = parse_lc_response(text)
        stamp = time.monotonic()
        activity.update(timestamp=datetime.now().astimezone(), duration=None)
        if activity['squelch_open']:
            self.active_transmission = (stamp, activity)
        elif self.active_transmission is not None:
            started, opened = self.active_transmission
            if ((activity['source'], activity['source_id']) ==
                    (opened['source'], opened['source_id']) and
                    (activity['frequency_hz'] is None or
                     activity['frequency_hz'] == opened['frequency_hz'])):
                activity['frequency_hz'] = opened['frequency_hz']
                activity['duration'] = stamp - started
                self.active_transmission = None
        self.update_signal(activity['level'], activity['squelch_open'])
        self.receive_activity_context(activity)
        if len(self.activity_events) == self.activity_events.maxlen:
            self.activity_list.DeleteItem(0)
        self.activity_events.append(activity)
        row = self.activity_list.InsertItem(self.activity_list.GetItemCount(),
                                            activity['timestamp'].strftime('%H:%M:%S.%f')[:-3])
        frequency = activity['frequency_hz']
        values = ('---' if frequency is None else display_frequency(frequency),
                  '%s %s' % (activity['source'], activity['source_id']),
                  str(activity['level']),
                  '---' if activity['duration'] is None else '%.2f s' % activity['duration'],
                  'OPEN' if activity['squelch_open'] else 'CLOSED')
        for column, value in enumerate(values, 1):
            self.activity_list.SetItem(row, column, value)
        self.activity_list.EnsureVisible(row)
        if self.current_list_view() == 'LOG VIEW':
            self.show_log_view()

    def __set_properties(self):
        self.SetTitle("Serial Terminal")
        # self.SetSize((546, 383))

    def __attach_events(self):
        # register events at the controls
        self.Bind(wx.EVT_TOOL, self.on_tool)
        self.cbx_lists.Bind(wx.EVT_MOUSEWHEEL, do_nothing)
        self.Bind(wx.EVT_LIST_ITEM_RIGHT_CLICK, self.open_menu, self.edit_list.list)
        self.edit_list.memory.Bind(dv.EVT_DATAVIEW_ITEM_CONTEXT_MENU, self.on_memory_context_menu)
        self.edit_list.memory.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED, self.on_memory_activate)
        self.edit_list.memory.Bind(wx.EVT_LEFT_DCLICK, self.on_memory_double_click)
        self.bt_settings.Bind(wx.EVT_BUTTON, self.open_operation_settings)
        self.sql.Bind(wx.EVT_SLIDER, self.on_level_squelch_change)
        self.tuning_panel.tune_arrows.Bind(wx.EVT_BUTTON, self.on_move_frequency)
        self.Bind(wx.EVT_BUTTON, self.on_enter_freq, self.tuning_panel.tune_benter)
        self.Bind(wx.EVT_COMBOBOX, self.on_select_list, self.cbx_lists)
        self.Bind(wx.EVT_BUTTON, self.on_select_list, self.bt_refresh)
        self.Bind(wx.EVT_CHECKBOX, self.on_auto, self.ckbx_auto)
        self.Bind(wx.EVT_CHECKBOX, self.on_enter_att, self.ckbx_att)
        self.Bind(wx.EVT_CHECKBOX, self.on_receiver_switch, self.ckbx_nl)
        self.Bind(wx.EVT_CHECKBOX, self.on_receiver_switch, self.ckbx_afc)
        self.bt_newchannel.Bind(wx.EVT_BUTTON, lambda event: self.open_memory_channel())
        self.Bind(wx.EVT_COMBOBOX, self.on_select_mode, self.cbx_mode)
        self.Bind(wx.EVT_COMBOBOX, self.on_select_step, self.cbx_step)
        self.Bind(wx.EVT_RADIOBOX, self.on_select_vfo, self.rb_vfos)
        self.Bind(wx.EVT_BUTTON, self.on_operation_start, self.bt_start)
        self.Bind(wx.EVT_BUTTON, self.on_operation_stop, self.bt_stop)
        self.cbx_scan.Bind(wx.EVT_COMBOBOX, lambda evt: self.request_group('scan'))
        self.cbx_search.Bind(wx.EVT_COMBOBOX, lambda evt: self.request_group('search'))
        self.bt_scgrp.Bind(wx.EVT_BUTTON, lambda evt: self.open_group('scan'))
        self.bt_search.Bind(wx.EVT_BUTTON, lambda evt: self.open_group('search'))
        self.operation_choice.Bind(wx.EVT_CHOICE, self.on_operation_changed)
        self.source_choice.Bind(wx.EVT_CHOICE, self.on_source_changed)
        self.memory_bank_choice.Bind(wx.EVT_CHOICE, self.on_memory_source_changed)
        self.search_bank_choice.Bind(wx.EVT_CHOICE, self.on_search_source_changed)
        self.bt_newsearchbank.Bind(wx.EVT_BUTTON, lambda evt: self.open_search_bank(create=True))
        self.bt_editsearchbank.Bind(wx.EVT_BUTTON, lambda evt: self.open_search_bank())
        self.Bind(wx.EVT_BUTTON, self.on_add_pass_frequency, self.bt_mkpassfreq)
        self.Bind(EVT_SERIALRX, self.on_serial_read)
        self.Bind(wx.EVT_CLOSE, self.on_close)

    def on_tool(self, evt):
        if evt.GetId() == self.config_tool_id:
            self.set_port()
        elif evt.GetId() == self.connect_tool_id:
            if self.serial.is_open:
                self.stop_thread()
                self.close_serial()
            else:
                self.connect()

    def open_menu(self, event):
        """"""
        view = self.current_list_view()
        if view in ('LOG VIEW', 'SELECT SCAN'):
            return
        if self.selected_memory_bank() is not None:
            self.open_memory_flag_menu(event.GetIndex())
            return
        if view == 'SEARCH BANKS':
            bank = self.edit_list.list.GetItemText(event.GetIndex())
            entry = self.search_banks.get(bank)
            if entry is None:
                return
            menu = wx.Menu()
            edit = menu.Append(wx.ID_ANY, 'Edit Stored Range...')
            edit.Enable(self.connected and self.serial.is_open)
            menu.Bind(wx.EVT_MENU, lambda evt: self.open_search_bank(bank=bank), id=edit.GetId())
            self.edit_list.list.PopupMenu(menu)
            menu.Destroy()
            return
        if view == 'PASS FREQS':
            row = event.GetIndex()
            slot = self.edit_list.list.GetItemText(row)
            entry = self.pass_frequency_rows.get(slot)
            menu = wx.Menu()
            choose = menu.Append(wx.ID_ANY, 'Choose pass-frequency bank / VFO...')
            menu.Bind(wx.EVT_MENU, self.on_choose_pass_context, id=choose.GetId())
            remove = menu.Append(wx.ID_ANY, 'Remove pass frequency')
            remove.Enable(self.connected and self.serial.is_open and self.alive.is_set()
                          and entry is not None and entry['frequency_hz'] is not None
                          and entry['context'] != 'V')
            menu.Bind(wx.EVT_MENU, lambda evt: self.remove_pass_frequency(slot),
                      id=remove.GetId())
            self.edit_list.list.PopupMenu(menu)
            menu.Destroy()
            return
    def on_move_frequency(self, evt):
        obj = evt.GetEventObject()
        name = obj.GetLabel()

        if name == '>':
            func = '\x1e\r\n'
        elif name == '<':
            func = '\x1f\r\n'
        elif name == '>>':
            func = '\x1c\r\n'
        elif name == '<<':
            func = '\x1d\r\n'
        else:
            return

        self.clear_vfo_channel()
        self.write_serial(func.encode("ascii"))
        self.write_serial('RX\r\n'.encode("ascii"))

    def on_auto(self, evt):
        self.clear_vfo_channel()
        if self.ckbx_auto.IsChecked():
            self.write_serial('AU1\r\nRX\r\n'.encode("ascii"))
        else:
            self.write_serial('AU0\r\n'.encode("ascii"))

    def on_enter_att(self, evt):
        """Changes Attenuation ON/OFF
        """
        att = 1 if self.ckbx_att.IsChecked() else 0
        self.clear_vfo_channel()
        self.write_serial(('AT%s\r\n' % att).encode("ascii"))

    def on_select_vfo(self, evt):
        """Select working vfo"""
        selection = self.rb_vfos.GetSelection()
        if selection in self.vfo_status:
            self.set_vfo_text(self.vfo_status[selection], selection)
        comm = []
        if selection == 0:
            if not self.vfb:
                self.background_vfo = 1
                comm.append('VB\r\nRX\r\n')
            comm.append('VA\r\n')
            if not self.vfa:
                comm.append('RX\r\n')
        elif selection == 1:
            if not self.vfa:
                self.background_vfo = 0
                comm.append('VA\r\nRX\r\n')
            comm.append('VB\r\n')
            if not self.vfb:
                comm.append('RX\r\n')
        else:
            comm.append('VF\r\n')
            if not self.vfo:
                comm.append('RX\r\n')

        towrite = ''.join(comm)
        if not towrite.endswith('RX\r\n'):
            towrite += 'RX\r\n'
        # print 'towrite ', towrite
        self.write_serial(towrite.encode("ascii"))

    def on_vfo_start(self, evt):
        if not self.connected or not self.serial.is_open:
            return
        if self.search_restore_command is not None:
            self.aor_status.SetStatusText('Stop the current range search before starting another.')
            return
        selection = self.range_mode.GetSelection()
        mode = None if selection == 0 else GUI_MODE_TO_MD[selection - 1]
        try:
            lower, upper, step = search_parameters(self.range_lower.GetValue(), self.range_upper.GetValue(),
                                                   self.range_step.GetValue(), mode)
        except ValueError as error:
            self.aor_status.SetStatusText(str(error))
            return
        restore = bytearray()
        for vfx, context in ((0, 'VA'), (1, 'VB'), (2, 'VF')):
            status = self.vfo_status.get(vfx)
            if status:
                fields = {field[:2]: field[2:] for field in status.split()[1:]}
                restore.extend(('%s\r\nRF%s\r\nAU0\r\nMD%s\r\nST%s\r\nAT%s\r\nAU%s\r\n' %
                                (context, fields['RF'], fields['MD'], fields['ST'], fields['AT'], fields['AU'])).encode('ascii'))
        self.search_restore_command = bytes(restore) + self.normal_vfo_command()
        command = 'VA\r\nRF%010d\r\nVB\r\nRF%010d\r\nVA\r\n' % (lower, upper)
        command += ('AU1\r\n' if mode is None else 'AU0\r\nMD%d\r\nST%06d\r\n' % (mode, step))
        data = (command + 'VS\r\nRX\r\n').encode('ascii')
        if self.write_serial(data) != len(data):
            self.search_restore_command = None
            return False
        self.aor_status.SetStatusText('VFO range search started; Stop Search restores the previous VFO settings.')
        return True

    def on_vfo_stop(self, evt):
        if not self.connected or not self.serial.is_open:
            return
        # VA/VB/VF select normal receiver operation. VV0 is VFO Scan, not Search stop.
        command = self.normal_vfo_command() + (self.search_restore_command or b'') + b'RX\r\n'
        self.search_restore_command = None
        self.write_serial(command)

    def on_select_scan_start(self, evt, source=None):
        if not self.connected or not self.serial.is_open:
            return
        if source == 'Select Scan' or (source is None and self.current_list_view() == 'SELECT SCAN'):
            sent = self.write_serial(b'SM\r\n') == 4
            self.aor_status.SetStatusText('Selected Channels start requested')
            return sent
        else:
            bank = self.choose_start_bank('scan')
            if bank:
                command = ('GM%s\r\nMS%s\r\nRX\r\n' % (self.cbx_scan.GetValue(), bank)).encode('ascii')
                return self.write_serial(command) == len(command)

    def normal_vfo_command(self):
        return (('VA', 'VB', 'VF')[self.rb_vfos.GetSelection()] + '\r\n').encode('ascii')

    def selected_memory_bank(self):
        index = self.cbx_lists.GetSelection()
        return self.cbx_lists.GetClientData(index) if index != wx.NOT_FOUND and self.cbx_lists.HasClientObjectData() else None

    def current_list_view(self):
        if self.selected_memory_bank() is not None:
            return ''
        label = self.cbx_lists.GetStringSelection()
        return next((key for key, display in LIST_LABELS.items() if display == label), label)

    def select_list_view(self, key):
        self.cbx_lists.SetStringSelection(LIST_LABELS[key])

    def request_group(self, kind):
        if not self.connected or not self.serial.is_open or self.group_loading[kind] is not None:
            return
        control = self.cbx_scan if kind == 'scan' else self.cbx_search
        group = int(control.GetValue())
        prefix = 'GM' if kind == 'scan' else 'GS'
        self.group_loading[kind] = group
        self.group_response[kind] = None
        self.write_serial(('%s%d\r\n%s\r\n' % (prefix, group, prefix)).encode('ascii'))
        self.update_connection_ui()

    def set_group_response(self, text):
        match = re.match(r'G([MS])([0-9])(?:\s|$)', text)
        if match:
            kind = 'scan' if match.group(1) == 'M' else 'search'
            self.group_response[kind] = int(match.group(2))
            if self.settings_dialog is not None:
                self.settings_dialog.accept_context(kind, int(match.group(2)))
            for parameter in re.findall(r'(?:[XS][AB][ +]?[0-9]{3}|[XS][D](?:[0-9]\.[0-9]|FF)|[XS]P[0-9]{2}|XM[0-8F])', text):
                self.accept_operation_parameter(parameter)
            return
        kind = 'scan' if text.startswith('BM') else 'search'
        members = parse_group_members(text, kind)
        group = self.group_response[kind]
        if group is None:
            raise ValueError('Group membership received without an identified group')
        self.groups[kind][group] = members
        control = self.cbx_scan if kind == 'scan' else self.cbx_search
        if self.group_loading[kind] in (None, group):
            control.SetStringSelection(str(group))
            self.group_loading[kind] = None
            self.aor_status.SetStatusText('%s Group %d: %s' %
                                         (kind.title(), group, 'LINK OFF' if group == 0 else ', '.join(members) or 'no linked banks'))
        dialog = self.group_dialogs[kind]
        if dialog is not None and dialog.group == group:
            dialog.show_members(members)
        self.update_connection_ui()

    def open_group(self, kind):
        if not self.connected:
            return
        if self.group_dialogs[kind] is not None:
            self.group_dialogs[kind].Raise()
            return
        control = self.cbx_scan if kind == 'scan' else self.cbx_search
        group = int(control.GetValue())
        dialog = GroupDialog(self, kind, group)
        self.group_dialogs[kind] = dialog
        dialog.Show()
        self.request_group(kind)

    def selected_search_bank(self):
        if self.operation_selection() == ('Frequency Search', 'Search Bank'):
            index = self.search_bank_choice.GetSelection()
            if index != wx.NOT_FOUND:
                return self.search_bank_choice.GetClientData(index)
        if self.current_list_view() == 'SEARCH BANKS':
            row = self.edit_list.list.GetFirstSelected()
            if row != wx.NOT_FOUND:
                bank = self.edit_list.list.GetItemText(row)
                if re.fullmatch(r'[A-Ta-t]', bank):
                    return bank

    def choose_start_bank(self, kind):
        group = int((self.cbx_scan if kind == 'scan' else self.cbx_search).GetValue())
        if self.group_loading[kind] is not None:
            self.aor_status.SetStatusText('Wait for group configuration before starting.')
            return None
        metadata = self.operation_memory_bank() if kind == 'scan' else None
        selected = metadata['bank'] if metadata else self.selected_search_bank() if kind == 'search' else None
        members = self.groups[kind].get(group, ())
        if selected and (group == 0 or not members or selected in members):
            return selected
        if members:
            return members[0]
        definitions = self.memory_banks if kind == 'scan' else self.search_banks
        banks = [bank for bank in sorted(definitions, key=lambda b: (b.upper(), b.islower()))
                 if kind == 'scan' or not definitions[bank]['empty']]
        if not banks:
            self.aor_status.SetStatusText('Read Memory Banks / STORED RANGES first, then choose a starting bank.')
            return None
        dialog = wx.SingleChoiceDialog(self, 'Select a starting bank (group 0 is LINK OFF):',
                                        '%s Group' % kind.title(), banks)
        try:
            return banks[dialog.GetSelection()] if dialog.ShowModal() == wx.ID_OK else None
        finally:
            dialog.Destroy()

    def on_group_search(self, event):
        if self.connected and self.serial.is_open:
            bank = self.choose_start_bank('search')
            if bank:
                command = ('GS%s\r\nSS%s\r\nRX\r\n' % (self.cbx_search.GetValue(), bank)).encode('ascii')
                return self.write_serial(command) == len(command)

    def open_search_bank(self, create=False, bank=None):
        if not self.connected:
            return
        if self.search_bank_dialog is not None:
            self.search_bank_dialog.Raise()
            return
        bank = bank or (None if create else self.selected_search_bank())
        if bank is None and not create:
            self.aor_status.SetStatusText('Choose STORED RANGES and select a row to edit.')
            return
        if bank is None:
            bank = next((b for b, entry in self.search_banks.items() if entry['empty']), 'A')
        self.search_bank_dialog = SearchBankDialog(self, bank)
        self.search_bank_dialog.Show()
        self.search_bank_dialog.on_bank(None)

    def on_start_search_bank(self, event, bank=None):
        bank = bank or self.selected_search_bank()
        entry = self.search_banks.get(bank)
        if self.connected and self.serial.is_open and entry is not None and not entry['empty']:
            command = ('SS%s\r\nRX\r\n' % bank).encode('ascii')
            return self.write_serial(command) == len(command)
        else:
            self.aor_status.SetStatusText('Choose STORED RANGES and select a populated row to start.')

    def on_select_mode(self, evt):
        """Set mode on RX
        """
        mode = self.cbx_mode.GetSelection()
        if not 0 <= mode < len(GUI_MODE_TO_MD):
            return
        self.clear_vfo_channel()
        self.write_serial(('MD%s\r\n' % GUI_MODE_TO_MD[mode]).encode("ascii"))
        self.afc_mode = GUI_MODE_TO_MD[mode]
        self.update_receiver_switch_ui()

    def on_select_step(self, evt):
        """Set step on RX
        STnnnnm0<CR> Set the tuning step size in Hz
        STnnn.nm<CR> Set the tuning step size in kHz
        """
        step = float(self.cbx_step.GetStringSelection())
        self.clear_vfo_channel()
        self.write_serial(('ST%06.2f\r\n' % step).encode("ascii"))

    def on_enter_freq(self, evt):
        """Writes command RF to serial
        """
        freq = self.tuning_panel.freq
        if not 0.1 < freq < 3000:
            return
        self.clear_vfo_channel()
        comm = 'RF%010.5f\r\n' % freq
        self.write_serial(comm.encode("ascii"))
        self.write_serial(b'RX\r\n')

    def on_select_list(self, evt):
        if self.memory_flag_edit is not None or self.memory_channel_pending is not None or self.memory_inventory is not None:
            self.aor_status.SetStatusText('Wait for the memory-channel flag read-back before loading another list.')
            return
        if self.selected_memory_bank() is not None:
            self.last_memory_bank = self.selected_memory_bank()['bank']
            self.sync_memory_choices(self.selected_memory_bank())
        selection = self.current_list_view()
        self.edit_list.show_memory(self.selected_memory_bank() is not None)
        self.update_connection_ui()
        if selection not in ('LOG VIEW', 'DATABASE') and not (
                self.connected and self.serial.is_open and self.alive.is_set()):
            self.aor_status.SetStatusText('Connect to read scanner lists; LOG VIEW is available offline.')
            return
        self.filling_banks = False
        self.pending_memory_channels.clear()
        if selection == 'SEARCH BANKS':
            self.get_search_banks()
        elif selection == 'SELECT SCAN':
            self.get_select_scan()
        elif selection == 'PASS FREQS':
            if evt is not None and evt.GetEventObject() == self.bt_refresh:
                self.on_choose_pass_context(evt)
            else:
                self.get_pass_frequencies()
        elif selection == 'LOG VIEW':
            self.show_log_view()
        elif selection == 'DATABASE':
            pass
        else:
            self.filling_banks = True
            self.get_memory_banks()

    def on_serial_read(self, event):
        """Handle input from the serial port."""
        received_valid = False
        for first in event.data:
            try:
                if not isinstance(first, str):
                    raise ValueError('Response must be text')
                if self.memory_inventory is not None and self.receive_memory_inventory_line(first):
                    received_valid = True
                    continue
                if self.bandscope is not None:
                    self.bandscope.capture_receiver(first)
                temporary_context = self.monitor_muted
                background = False
                # print 'event text ', text
                if first.startswith('LM'):
                    self.set_signal_level(first)
                elif first.startswith(('NL', 'AF')):
                    self.set_receiver_switch(first)
                elif first.startswith('PH'):
                    if self.bandscope is not None:
                        self.bandscope.set_peak_hold(first)
                elif first.startswith(('DA', 'DB', 'DD', 'DP', 'XA', 'XB', 'XD', 'XM', 'XP', 'SA', 'SB', 'SD', 'SP')):
                    self.accept_operation_parameter(first)
                elif first.startswith('AM '):
                    if self.bandscope is not None:
                        self.bandscope.set_status(first)
                elif first.startswith('DS'):
                    if self.bandscope is not None:
                        self.bandscope.add_ds_line(first)
                elif first.startswith('LC'):
                    if first in ('LC0', 'LC1'):
                        # Configuration replies are not reception events.
                        continue
                    self.record_activity(first)
                elif first.startswith(('VA ', 'VB ', 'VF ')):
                    vfx = {'VA': 0, 'VB': 1, 'VF': 2}[first[:2]]
                    background = vfx == self.background_vfo
                    self.set_vfo_text(first, vfx, active=not background)
                    if background:
                        self.background_vfo = None
                    elif not self.connected and self.background_vfo is None and vfx in (0, 1):
                        self.background_vfo = 1 - vfx
                        command = 'VA\r\nRX\r\nVB\r\n' if vfx == 1 else 'VB\r\nRX\r\nVA\r\n'
                        self.write_serial(command.encode("ascii"))
                elif first.startswith(('GM', 'GS', 'BM', 'BS')):
                    self.set_group_response(first)
                elif first.startswith(('VS ', 'VV ')):
                    match = re.fullmatch(r'V[SV] (V[ABF]) (RF.*)', first)
                    if match is None:
                        raise ValueError('Invalid VFO search/scan status')
                    self.set_vfo_text(match.group(1) + ' ' + match.group(2),
                                      {'VA': 0, 'VB': 1, 'VF': 2}[match.group(1)])
                elif re.match(r'SR[A-Ta-t] RF', first):
                    fields = first.split(None, 6)
                    self.validate_fields(fields[1:6], ('RF', 'ST', 'AU', 'MD', 'AT'))
                    if len(fields) != 7 or not fields[6].startswith('TT'):
                        raise ValueError('Invalid active Search Bank status')
                    self.aor_status.SetStatusText('Stored Range %s: %s' % (first[2], display_frequency(protocol_frequency_hz(fields[1][2:]))))
                elif first.startswith('SR'):
                    self.set_search_banks(first)
                elif first.startswith('GR'):
                    self.set_select_scan(first)
                elif first.startswith('PR'):
                    self.set_pass_frequency(first)
                elif first.startswith('SM '):
                    fields = first.split(None, 8)[1:]
                    self.validate_fields(fields, ('MX', 'MP', 'RF', 'ST', 'AU', 'MD', 'AT', 'TM'))
                    self.aor_status.SetStatusText('Selected Channels: %s, %s' %
                                                (fields[0][2:], display_frequency(protocol_frequency_hz(fields[2][2:]))))
                elif first.startswith('MW'):
                    self.set_memory_banks_list(first)
                elif first.startswith('MX'):
                    if self.memory_channel_pending is not None:
                        self.receive_memory_channel_line(first)
                    elif self.filling_banks:
                        self.set_memory_banks(first)
                    else:
                        continue
                elif first.startswith(('MR ', 'MS ')):
                    fields = first.split(None, 8)[1:]
                    self.validate_fields(fields, ('MX', 'MP', 'RF', 'ST', 'AU', 'MD', 'AT', 'TM'))
                    self.aor_status.SetStatusText('%s: %s, %s' %
                                                (first[:2], fields[0][2:], display_frequency(protocol_frequency_hz(fields[2][2:]))))
                elif first in ('GA0', 'GA1', 'MP0', 'MP1', 'MC0', 'MC1'):
                    # Acknowledgements are not a substitute for channel read-back.
                    continue
                else:
                    print('Ignored unexpected scanner response: %r' % first, file=sys.stderr)
                    continue

                if first.startswith(('VA ', 'VB ', 'VF ', 'MR ', 'MS ', 'SM ', 'VS ', 'VV ')) or re.match(r'SR[A-Ta-t] RF', first):
                    self.receive_memory_delete_status(first)
                    self.on_memory_flag_status(first)
                    if not temporary_context and not background:
                        self.receive_context_status(first)
                        self.update_operation_ui()
                    if self.memory_inventory is not None:
                        self.receive_memory_inventory_context(first)
                received_valid = True
            except (ValueError, IndexError, TypeError) as error:
                if self.memory_inventory is not None and isinstance(first, str) and first.startswith(('MW', 'MX', 'GR')):
                    self.finish_memory_inventory('Export failed: malformed scanner response %r: %s' % (first, error))
                if isinstance(first, str) and first.startswith('GR') and self.memory_select_read is not None:
                    self.memory_select_read['invalid'] = True
                print('Ignored malformed scanner response %r: %s' % (first, error), file=sys.stderr)
        if received_valid and self.serial.is_open and self.alive.is_set() and not self.connected:
            self.connected = True
            self.start_monitoring()
            self.aor_status.SetStatusText('Select a VFO or list, or enter a frequency in MHz.')
            self.update_connection_ui()
            self.show_now_receiving()
        self.maybe_load_initial_bank()

    def write_serial(self, data):
        if self.memory_inventory is not None and any(
                re.fullmatch(rb'RX|TB|GR|LM|MA[A-Ja-j]?', command) is None
                for command in data.split(b'\r\n') if command):
            self.aor_status.SetStatusText('Inventory export is read-only; wait for it to finish before changing scanner settings.')
            return
        try:
            return self.serial.write(data)
        except serial.SerialException as error:
            print('Serial write error: %s' % error, file=sys.stderr)

    def close_serial(self):
        self.stop_monitoring()
        self.connected = False
        try:
            self.serial.close()
        except serial.SerialException as error:
            print('Serial close error: %s' % error, file=sys.stderr)
        self.update_connection_ui()
        if not self.serial.is_open:
            self.SetTitle('Serial Terminal')
            self.aor_status.SetStatusText('Choose Serial Config, then Connect.')

    def validate_fields(self, fields, prefixes):
        if len(fields) != len(prefixes):
            raise ValueError('Unexpected field count')
        for field, prefix in zip(fields, prefixes):
            if not field.startswith(prefix):
                raise ValueError('Unexpected field prefix')
            value = field[2:]
            if prefix in ('RF', 'ST', 'SL', 'SU'):
                if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value):
                    raise ValueError('Invalid numeric field')
                if prefix in ('RF', 'SL', 'SU') and not re.fullmatch(r'[0-9]{10}|[0-9]{4}\.[0-9]{4,5}', value):
                    raise ValueError('Incomplete frequency field')
            elif prefix in ('AU', 'AT', 'MP'):
                if value not in ('0', '1'):
                    raise ValueError('Invalid boolean field')
            elif prefix == 'MD':
                if not value.isascii() or not value.isdigit() or int(value) not in MD_TO_GUI_MODE:
                    raise ValueError('Invalid mode field')
            elif prefix == 'MX':
                if not re.fullmatch(r'[A-Ta-t][0-9]{2}', value):
                    raise ValueError('Invalid memory channel')

    def log_new(self, event):  # wxGlade: AorCtrlFrame.<event_handler>
        pass

    def log_open(self, event):  # wxGlade: AorCtrlFrame.<event_handler>
        pass

    # noinspection PyPep8Naming
    def OnExit(self, event):
        """Menu point Exit"""
        self.Close()

    def on_close(self, event):
        """Called on application shutdown."""
        self.stop_thread()               # stop reader thread
        self.close_serial()             # cleanup
        self.Destroy()                  # close windows, exit app

    def set_port(self):
        """Show the port settings dialog. The reader thread is stopped for the
           settings change.
        """
        self.stop_thread()
        self.close_serial()

        dialog_serial_cfg = serial_conf_dialog.SerialConfigDialog(None, -1, "", serial=self.serial)
        dialog_serial_cfg.ShowModal()
        dialog_serial_cfg.Destroy()

    def connect(self):
        """"""
        self.stop_thread()
        self.close_serial()

        try:
            self.serial.open()
        except serial.SerialException as e:
            dlg = wx.MessageDialog(None, str(e), "Serial Port Error", wx.OK | wx.ICON_ERROR)
            dlg.ShowModal()
            dlg.Destroy()
        else:
            self.start_thread()
            self.SetTitle("Serial Terminal on %s [%s, %s%s%s%s%s]" % (
                self.serial.portstr,
                self.serial.baudrate,
                self.serial.bytesize,
                self.serial.parity,
                self.serial.stopbits,
                self.serial.rtscts and ' RTS/CTS' or '',
                self.serial.xonxoff and ' Xon/Xoff' or '',
                )
            )
            self.connected = False
            self.update_connection_ui()
            self.memory_banks.clear()
            self.initial_bank_load_pending = True
            self.group_loading = {'scan': None, 'search': None}
            self.group_response = {'scan': None, 'search': None}
            self.write_serial('RX\r\n'.encode("ascii"))
            self.write_serial('TB\r\n'.encode("ascii"))
            self.write_serial('TB\r\n'.encode("ascii"))

    def get_memory_banks(self):
        """
        MA[bank] starts at channels 00-09; bare MA advances ten channels.
        Populated channels return MX[bank][channel] MP RF ST AU MD AT TM.
        Empty channels return MX[bank][channel] ---.
        """
        metadata = self.selected_memory_bank()
        if metadata is None:
            self.filling_banks = False
            return
        bank, channels = metadata['bank'], metadata['channels']
        self.row = 0
        self.last = None
        self.memory_rows.clear()
        self.memory_empty_channels.clear()
        self.memory_select_read = None
        self.edit_list.memory_model.clear()
        self.edit_list.show_memory(True)

        channel_count = channels
        blocks = (channel_count + 9) // 10
        self.pending_memory_channels = {'%s%02i' % (bank, channel) for channel in range(channel_count)}
        self.filling_banks = bool(self.pending_memory_channels)
        if blocks:
            towrite = 'MA%s\r\n' % bank + 'MA\r\n' * (blocks - 1)
            self.write_serial(towrite.encode("ascii"))
        else:
            self.read_memory_select_flags()

    def show_memory_flags(self, channel):
        entry = self.memory_rows[channel]
        if entry['row'] < len(self.edit_list.memory_model.channels):
            self.edit_list.memory_model.RowChanged(entry['row'])

    def memory_row_values(self, channel):
        entry = self.memory_rows[channel]
        fields = entry['fields']
        return [channel, entry['select'], entry['skip'], display_frequency(protocol_frequency_hz(fields[2])),
                display_step(protocol_step_hz(fields[3])), 'On' if fields[4] == '1' else 'Off',
                self.cbx_mode.GetString(MD_TO_GUI_MODE[int(fields[5])]), 'On' if fields[6] == '1' else 'Off', fields[7]]

    def read_memory_select_flags(self):
        """GR supplies tags missing from MA; the following RX bounds the list."""
        if self.memory_select_read is not None or not self.connected or not self.serial.is_open:
            return
        self.memory_select_read = {'channels': set(), 'slots': {}, 'seen': False, 'invalid': False}
        self.memory_flag_timer.StartOnce(10000)
        if self.write_serial(b'GR\r\nRX\r\n') != 8:
            self.on_memory_flag_timeout(None)

    def memory_inventory_available(self):
        return (self.connected and self.serial.is_open and self.alive.is_set() and self.memory_inventory is None and
                not self.filling_banks and self.memory_select_read is None and self.memory_flag_edit is None and
                self.memory_channel_pending is None and self.background_vfo is None and not self.monitor_muted and
                self.active_operation is None and self.settings_dialog is None and
                (self.bandscope is None or not self.bandscope.entered and self.bandscope.waiting is None))

    def on_export_memory_inventory(self, event):
        if not self.memory_inventory_available():
            self.aor_status.SetStatusText('Connect and wait for current scanner operations to finish before exporting.')
            return
        dialog = wx.FileDialog(self, 'Export Memory Inventory', defaultFile='AR8600_MEMORY_INVENTORY.csv',
                               wildcard='CSV files (*.csv)|*.csv', style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = dialog.GetPath()
        finally:
            dialog.Destroy()
        self.start_memory_inventory(path)

    def start_memory_inventory(self, path):
        if not self.memory_inventory_available():
            self.aor_status.SetStatusText('Export could not start: receiver is disconnected or busy.')
            return
        self.memory_inventory = {'path': path, 'phase': 'banks', 'banks': {}, 'rows': {}, 'selected': set(),
                                 'slots': {}, 'context': None, 'order': sorted('ABCDEFGHIJabcdefghij',
                                  key=lambda bank: (bank.upper(), bank.islower())), 'index': 0}
        self.update_connection_ui()
        self.aor_status.SetStatusText('Export: reading all 20 memory bank names and capacities...')
        self.send_memory_inventory(b'RX\r\nTB\r\nTB\r\n')

    def send_memory_inventory(self, command):
        self.inventory_timer.StartOnce(20000)
        if self.write_serial(command) != len(command):
            self.finish_memory_inventory('Export failed: serial write/read request failed.')

    def receive_memory_inventory_line(self, text):
        inventory = self.memory_inventory
        if text.startswith('MW'):
            metadata = self.parse_memory_bank_line(text)
            if inventory['phase'] == 'banks':
                bank = metadata['bank']
                previous = inventory['banks'].get(bank)
                if previous is not None and previous != metadata:
                    raise ValueError('Conflicting bank metadata during export')
                inventory['banks'][bank] = metadata
                if len(inventory['banks']) == 20:
                    self.read_next_inventory_bank()
            return True
        if text.startswith('MX'):
            channel, fields = self.parse_memory_line(text)
            if inventory['phase'] == 'channels' and channel in inventory['remaining']:
                inventory['remaining'].remove(channel)
                if fields is not None and int(channel[1:]) < inventory['banks'][channel[0]]['channels']:
                    inventory['rows'][channel] = fields
                if not inventory['remaining']:
                    self.read_next_inventory_bank()
            return True
        if text.startswith('GR'):
            entry = parse_select_scan_response(text)
            if inventory['phase'] == 'selected':
                slot = int(entry['slot'])
                if slot in inventory['slots'] and inventory['slots'][slot] != entry['channel']:
                    raise ValueError('Conflicting Selected membership during export')
                inventory['slots'][slot] = entry['channel']
                if entry['channel'] is not None:
                    inventory['selected'].add(entry['channel'])
            return True
        return False

    def read_next_inventory_bank(self):
        inventory = self.memory_inventory
        while inventory['index'] < len(inventory['order']):
            bank = inventory['order'][inventory['index']]
            inventory['index'] += 1
            capacity = inventory['banks'][bank]['channels']
            if capacity == 0:
                continue
            blocks = (capacity + 9) // 10
            inventory['phase'] = 'channels'
            inventory['remaining'] = {'%s%02d' % (bank, number) for number in range(blocks * 10)}
            self.aor_status.SetStatusText('Export: reading bank %s (%d/20)...' % (bank, inventory['index']))
            self.send_memory_inventory(memory_channel_read_command('%s%02d' % (bank, capacity - 1)))
            return
        inventory['phase'] = 'selected'
        self.aor_status.SetStatusText('Export: reading Selected membership and confirming receiver context...')
        self.send_memory_inventory(b'GR\r\nRX\r\n')

    def receive_memory_inventory_context(self, text):
        inventory = self.memory_inventory
        if inventory['context'] is None:
            if inventory['phase'] == 'selected':
                self.finish_memory_inventory('Export failed: initial receiver context was not received.')
                return
            inventory['context'] = text
        if inventory['phase'] != 'selected':
            return
        slots = inventory['slots']
        if not slots or set(slots) != set(range(max(slots) + 1)):
            self.finish_memory_inventory('Export failed: incomplete Selected membership read-back.')
        elif not inventory['selected'].issubset(inventory['rows']):
            self.finish_memory_inventory('Export failed: Selected membership differs from channel inventory; retry the export.')
        elif text != inventory['context']:
            self.finish_memory_inventory('Export failed: receiver context changed while reading; retry the export.')
        else:
            self.write_memory_inventory_csv()

    def write_memory_inventory_csv(self):
        inventory = self.memory_inventory
        temporary_path = None
        counts = {}
        try:
            with tempfile.NamedTemporaryFile(mode='w', newline='', encoding='utf-8-sig', delete=False,
                                             dir=os.path.dirname(os.path.abspath(inventory['path'])),
                                             prefix='.ar8600_inventory_', suffix='.tmp') as output:
                temporary_path = output.name
                writer = csv.writer(output)
                writer.writerow(('Bank', 'BankName', 'Capacity', 'Channel', 'Frequency', 'Step', 'Auto',
                                 'Mode', 'Att', 'Skip', 'Selected', 'Name'))
                for bank in inventory['order']:
                    metadata = inventory['banks'][bank]
                    channels = sorted(channel for channel in inventory['rows'] if channel[0] == bank)
                    counts[bank] = len(channels)
                    common = (bank, metadata['name'], metadata['channels'])
                    if not channels:
                        writer.writerow(common + ('',) * 9)  # Keep empty banks and their allocated capacity.
                    for channel in channels:
                        fields = inventory['rows'][channel]
                        writer.writerow(common + (channel, display_frequency(protocol_frequency_hz(fields[2])),
                                                   display_step(protocol_step_hz(fields[3])), fields[4],
                                                   self.cbx_mode.GetString(MD_TO_GUI_MODE[int(fields[5])]), fields[6],
                                                   fields[1], int(channel in inventory['selected']), fields[7]))
            os.replace(temporary_path, inventory['path'])
            temporary_path = None
            self.finish_memory_inventory('Export complete: 20 banks, %d populated channels, %d empty banks. %s' %
                                         (sum(counts.values()), sum(count == 0 for count in counts.values()), inventory['path']))
        except OSError as error:
            self.finish_memory_inventory('Export failed: could not save CSV: %s' % error)
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError as error:
                    print('Could not remove inventory temporary file: %s' % error, file=sys.stderr)

    def finish_memory_inventory(self, message):
        self.memory_inventory = None
        self.inventory_timer.Stop()
        self.update_connection_ui()
        self.aor_status.SetStatusText(message)

    def memory_channel_available(self):
        return (self.selected_memory_bank() is not None and self.connected and self.serial.is_open and
                self.alive.is_set() and not self.filling_banks and self.memory_select_read is None and
                self.memory_channel_pending is None and self.memory_flag_edit is None and
                self.memory_inventory is None and
                not self.monitor_muted and self.active_operation is None and self.background_vfo is None and
                self.settings_dialog is None and
                (self.bandscope is None or not self.bandscope.entered and self.bandscope.waiting is None))

    def open_memory_channel(self, channel=None):
        if self.memory_channel_dialog is not None:
            self.memory_channel_dialog.Raise()
            return
        if not self.memory_channel_available() or channel is None and not self.memory_empty_channels:
            self.aor_status.SetStatusText('Load a Memory Bank and wait for its read-back before editing channels.')
            return
        self.memory_channel_dialog = MemoryChannelDialog(self, channel)
        self.memory_channel_dialog.Show()

    def save_memory_channel(self, channel, command, new=False):
        if not self.memory_channel_available():
            return
        bank = self.selected_memory_bank()
        if channel[0] != bank['bank'] or not 0 <= int(channel[1:]) < bank['channels']:
            if self.memory_channel_dialog is not None:
                self.memory_channel_dialog.message.SetLabel('Select this channel\'s Memory Bank before saving.')
            return
        if new and channel not in self.memory_empty_channels or not new and channel not in self.memory_rows:
            if self.memory_channel_dialog is not None:
                self.memory_channel_dialog.message.SetLabel('Channel availability changed; refresh the Memory Bank.')
            return
        self.memory_channel_pending = {'channel': channel, 'action': 'write', 'command': command,
                                       'new': new, 'stage': 'before', 'original_flags': None, 'restore': None}
        if self.memory_channel_dialog is not None:
            self.memory_channel_dialog.set_pending(True)
            self.memory_channel_dialog.message.SetLabel('Checking the channel before writing...')
        self.read_memory_channel()

    def delete_memory_channel(self, channel):
        if not self.memory_channel_available() or channel not in self.memory_rows:
            return
        answer = wx.MessageBox('Delete %s — %s from the scanner?\nThis cannot be undone.' %
                               (channel, self.memory_rows[channel]['fields'][7]), 'Delete Channel',
                               wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self)
        if answer != wx.YES:
            return
        self.memory_channel_pending = {'channel': channel, 'action': 'delete', 'phase': 'capture',
                                       'stage': 'after', 'restore': None, 'original_flags': None}
        self.memory_channel_timer.StartOnce(20000)
        self.update_connection_ui()
        if self.write_serial(b'RX\r\n') != 4:
            self.finish_memory_channel_edit('Could not capture receiver state; channel was not deleted.')

    def receive_memory_delete_status(self, text):
        pending = self.memory_channel_pending
        if pending is None or pending['action'] != 'delete':
            return
        if pending['phase'] == 'capture':
            if text.startswith(('VA ', 'VB ', 'VF ')):
                pending['restore'] = (text[:2] + '\r\n').encode('ascii')
            elif text.startswith('MR '):
                current = text.split()[1][2:]
                # The deleted active memory cannot be recalled; return to the selected VFO.
                pending['restore'] = (('MR%s\r\n' % current).encode('ascii') if current != pending['channel'] else
                                      self.normal_vfo_command())
            else:
                self.finish_memory_channel_edit('Stop scanning/searching before deleting a channel.')
                return
            pending['phase'] = 'recall'
            self.monitor_muted = True
            command = ('MC1\r\nMR%s\r\nRX\r\n' % pending['channel']).encode('ascii')
            if self.write_serial(command) != len(command):
                self.finish_memory_channel_edit('Could not confirm the channel; it was not deleted.')
        elif pending['phase'] == 'recall' and text.startswith('MR MX' + pending['channel'] + ' '):
            # MQ with no argument deletes only the validated current M.RD channel.
            command = b'MQ\r\n' + pending['restore'] + b'MC0\r\nRX\r\n'
            if self.write_serial(command) != len(command):
                self.finish_memory_channel_edit('Delete write failed; channel state was not verified.')
                return
            pending['restore'] = None
            self.monitor_muted = False
            self.read_memory_channel()

    def read_memory_channel(self):
        pending = self.memory_channel_pending
        channel = pending['channel']
        pending['phase'] = 'readback'
        # Drain all requested blocks, including rows before/after the target.
        pending['remaining'] = {'%s%02d' % (channel[0], number)
                                for number in range((int(channel[1:]) // 10 + 1) * 10)}
        pending['fields'] = None
        self.memory_channel_timer.StartOnce(20000)
        self.update_connection_ui()
        command = memory_channel_read_command(channel)
        if self.write_serial(command) != len(command):
            self.finish_memory_channel_edit('Channel read-back failed; refresh the bank to check scanner state.')

    def receive_memory_channel_line(self, text):
        pending = self.memory_channel_pending
        if pending['phase'] != 'readback':
            return
        channel, fields = self.parse_memory_line(text)
        if channel not in pending['remaining']:
            return
        pending['remaining'].remove(channel)
        if channel == pending['channel']:
            pending['fields'] = fields
        if pending['remaining']:
            return
        channel, fields = pending['channel'], pending['fields']
        if fields is None:
            self.memory_empty_channels.add(channel)
            if channel in self.memory_rows:
                self.edit_list.memory_model.remove(channel)
        else:
            self.memory_empty_channels.discard(channel)
            entry = self.memory_rows.get(channel)
            if entry is None:
                self.memory_rows[channel] = {'fields': fields, 'skip': fields[1] == '1', 'select': None, 'row': 0}
                self.edit_list.memory_model.insert(channel)
            else:
                entry.update(fields=fields, skip=fields[1] == '1')
                self.show_memory_flags(channel)
        if pending['stage'] == 'before':
            if pending['new']:
                if fields is not None:
                    self.finish_memory_channel_edit('Channel is no longer empty; nothing was overwritten.')
                else:
                    self.commit_memory_channel_write()
            elif fields is None:
                self.finish_memory_channel_edit('Channel no longer exists; nothing was written.')
            else:
                pending['phase'] = 'flags'
                self.read_memory_select_flags()
        elif pending['action'] == 'write' and fields is None:
            self.finish_memory_channel_edit('Write did not create a populated channel; check protection/serial diagnostics.')
        else:
            pending['phase'] = 'flags'
            self.read_memory_select_flags()

    def commit_memory_channel_write(self):
        pending = self.memory_channel_pending
        pending['stage'] = 'after'
        if self.memory_channel_dialog is not None:
            self.memory_channel_dialog.message.SetLabel('Writing channel and waiting for scanner read-back...')
        if self.write_serial(pending['command']) != len(pending['command']):
            self.finish_memory_channel_edit('Channel write failed; state was not verified.')
            return
        self.read_memory_channel()

    def continue_memory_channel_edit(self):
        pending = self.memory_channel_pending
        if pending is None:
            return
        channel = pending['channel']
        if pending['stage'] == 'before':
            pending['original_flags'] = {flag: self.memory_rows[channel][flag] for flag in ('select', 'skip')}
            self.commit_memory_channel_write()
            return
        if pending['action'] == 'delete':
            self.finish_memory_channel_edit('Channel %s %s' %
                                           (channel, 'deleted and verified.' if pending['fields'] is None else
                                            'was not deleted; scanner-returned channel is still present.'))
            return
        entry = self.memory_rows[channel]
        if pending['original_flags'] is not None:
            for flag, original in pending['original_flags'].items():
                if entry[flag] != original:
                    pending['phase'] = 'preserve'
                    self.toggle_memory_flag(channel, flag, preserving=True)
                    return
        # Compare supplied parameters; Auto's final mode/step come from the scanner.
        prefix, _, name = pending['command'].decode('ascii').rstrip('\r\n').partition(' TM')
        expected = {field[:2]: field[2:] for field in prefix.split()[1:]}
        actual = dict(zip(('MX', 'MP', 'RF', 'ST', 'AU', 'MD', 'AT', 'TM'), entry['fields']))
        matched = actual['TM'].rstrip() == name.rstrip()
        for key, value in expected.items():
            parse = protocol_frequency_hz if key == 'RF' else protocol_step_hz if key == 'ST' else int
            matched = matched and parse(actual[key]) == parse(value)
        if self.vfo_channel is not None and self.vfo_channel['channel'] == channel:
            self.clear_vfo_channel()
        self.finish_memory_channel_edit('Channel %s %s' %
                                       (channel, 'saved and verified; Selected/Skip retained.' if matched else
                                        'read-back differs from the requested data; showing scanner values.'))

    def finish_memory_channel_edit(self, message):
        pending = self.memory_channel_pending
        self.memory_channel_pending = None
        self.memory_channel_timer.Stop()
        if (pending is not None and pending['action'] == 'delete' and pending.get('fields') is None and
                pending['phase'] == 'flags' and self.vfo_channel is not None and
                self.vfo_channel['channel'] == pending['channel']):
            self.vfo_channel = None
        if pending is not None and pending['restore'] is not None and self.serial.is_open:
            try:
                self.write_serial(pending['restore'])
            finally:
                self.release_monitor_mute()
                self.write_serial(b'RX\r\n')
        if self.memory_channel_dialog is not None:
            if pending is not None and pending.get('fields') is not None and pending['stage'] == 'after':
                self.memory_channel_dialog.existing = True
                self.memory_channel_dialog.show_channel(pending['fields'])
            self.memory_channel_dialog.set_pending(False)
            self.memory_channel_dialog.message.SetLabel(message)
        self.aor_status.SetStatusText(message)
        self.show_now_receiving()
        self.update_connection_ui()

    def on_memory_channel_timeout(self, event):
        self.finish_memory_channel_edit('Channel operation timed out; refresh the bank to check scanner state.')
        print('Memory channel operation timed out', file=sys.stderr)

    def memory_flags_editable(self, channel, preserving=False):
        return (self.selected_memory_bank() is not None and channel in self.memory_rows and
                self.connected and self.serial.is_open and self.alive.is_set() and
                not self.filling_banks and self.memory_select_read is None and
                self.memory_flag_edit is None and self.memory_rows[channel]['select'] is not None and
                self.memory_inventory is None and
                (self.memory_channel_pending is None or preserving and self.memory_channel_pending['phase'] == 'preserve') and
                not self.monitor_muted and self.settings_dialog is None and
                self.memory_rows[channel]['skip'] is not None and
                self.active_operation is None and self.background_vfo is None and
                (self.bandscope is None or (not self.bandscope.entered and self.bandscope.waiting is None)))

    def on_memory_context_menu(self, event):
        if event.GetItem().IsOk():
            self.open_memory_flag_menu(self.edit_list.memory_model.GetRow(event.GetItem()))

    def open_memory_flag_menu(self, row):
        channel = self.edit_list.memory_model.channels[row]
        entry = self.memory_rows.get(channel)
        if entry is None:
            return
        menu = wx.Menu()
        edit = menu.Append(wx.ID_ANY, 'Edit Channel...')
        edit.Enable(self.memory_channel_available())
        menu.Bind(wx.EVT_MENU, lambda evt: self.open_memory_channel(channel), id=edit.GetId())
        delete = menu.Append(wx.ID_ANY, 'Delete Channel...')
        delete.Enable(self.memory_channel_available())
        menu.Bind(wx.EVT_MENU, lambda evt: self.delete_memory_channel(channel), id=delete.GetId())
        menu.AppendSeparator()
        for flag, label in (('select', 'Include in Selected Channels'), ('skip', 'Skip during Stored Channels scanning')):
            item = menu.AppendCheckItem(wx.ID_ANY, label)
            item.Check(bool(entry[flag]))
            item.Enable(self.memory_flags_editable(channel))
            menu.Bind(wx.EVT_MENU, lambda evt, flag=flag: self.toggle_memory_flag(channel, flag), id=item.GetId())
        try:
            self.edit_list.memory.PopupMenu(menu)
        finally:
            menu.Destroy()

    def toggle_memory_flag(self, channel, flag, preserving=False):
        if flag not in ('select', 'skip') or not self.memory_flags_editable(channel, preserving=preserving):
            self.aor_status.SetStatusText('Wait for memory flags to load and stop scanning/bandscope before editing.')
            return
        self.memory_flag_edit = {'channel': channel, 'flag': flag,
                                 'desired': not self.memory_rows[channel][flag],
                                 'phase': 'capture', 'restore': None, 'restored': False}
        self.memory_flag_timer.StartOnce(10000)
        self.aor_status.SetStatusText('Reading receiver state before editing %s...' % channel)
        self.update_connection_ui()
        if self.write_serial(b'RX\r\n') != 4:
            self.finish_memory_flag_edit('Could not read receiver state; flag was not changed.')

    def on_memory_flag_status(self, text):
        """Only validated RX statuses advance a flag edit or finish a GR list."""
        if self.memory_select_read is not None:
            snapshot = self.memory_select_read
            self.memory_select_read = None
            self.memory_flag_timer.Stop()
            slots = snapshot['slots']
            contiguous = bool(slots) and set(slots) == set(range(max(slots) + 1))
            if snapshot['seen'] and contiguous and not snapshot['invalid']:
                for channel, entry in self.memory_rows.items():
                    entry['select'] = channel in snapshot['channels']
                    self.show_memory_flags(channel)
                edit = self.memory_flag_edit
                if edit is not None and edit['phase'] == 'verify_select':
                    actual = edit['channel'] in snapshot['channels']
                    self.finish_memory_flag_edit('%s Selected %s' %
                                                 (edit['channel'], 'saved and verified' if actual == edit['desired'] else 'verification failed'), verified=True)
            else:
                for channel, entry in self.memory_rows.items():
                    entry['select'] = None
                    self.show_memory_flags(channel)
                if self.memory_flag_edit is not None:
                    self.finish_memory_flag_edit('Selected Channels read-back failed; flag could not be verified.')
                else:
                    self.aor_status.SetStatusText('Selected flags unavailable: incomplete or malformed GR list. Refresh to retry.')
            if self.memory_channel_pending is not None and self.memory_channel_pending['phase'] == 'flags':
                if snapshot['seen'] and contiguous and not snapshot['invalid']:
                    self.continue_memory_channel_edit()
                else:
                    self.finish_memory_channel_edit('Channel flags could not be verified. Refresh to retry.')
            self.update_connection_ui()
        edit = self.memory_flag_edit
        if edit is None:
            return
        if edit['phase'] == 'capture':
            if not text.startswith(('VA ', 'VB ', 'VF ', 'MR ')):
                self.finish_memory_flag_edit('Stop scanning/searching before editing memory-channel flags.')
                return
            edit['restore'] = ((text[:2] + '\r\n') if text.startswith(('VA ', 'VB ', 'VF ')) else
                               ('MR%s\r\n' % text.split()[1][2:])).encode('ascii')
            edit['phase'] = 'recall'
            self.monitor_muted = True
            command = ('MC1\r\nMR%s\r\nRX\r\n' % edit['channel']).encode('ascii')
        elif text.startswith('MR ') and text.split()[1] == 'MX' + edit['channel']:
            entry = self.memory_rows[edit['channel']]
            fields = [field[2:] for field in text.split(None, 8)[1:]]
            entry['fields'] = fields
            entry['skip'] = fields[1] == '1'
            self.show_memory_flags(edit['channel'])
            if edit['phase'] == 'recall':
                edit['phase'] = 'verify_' + edit['flag']
                command = ('%s%d\r\n' % ('GA' if edit['flag'] == 'select' else 'MP', edit['desired'])).encode('ascii')
                if edit['flag'] == 'skip':
                    command += b'RX\r\n'
                else:
                    # GR is independent of M.RD: release audio before its list read-back.
                    command += edit['restore'] + b'MC0\r\n'
            elif edit['phase'] == 'verify_skip':
                self.finish_memory_flag_edit('%s Skip %s' %
                                             (edit['channel'], 'saved and verified' if entry['skip'] == edit['desired'] else 'verification failed'), verified=True)
                return
            else:
                return
        else:
            return  # Never change a flag until the requested memory is confirmed.
        if self.write_serial(command) != len(command):
            self.finish_memory_flag_edit('Serial write failed; memory flag was not verified.')
        elif edit['phase'] == 'verify_select':
            edit['restored'] = True
            self.monitor_muted = False
            self.read_memory_select_flags()

    def finish_memory_flag_edit(self, message, verified=False):
        edit = self.memory_flag_edit
        if edit is not None and edit['phase'].startswith('verify_') and not verified:
            self.memory_rows[edit['channel']][edit['flag']] = None
            self.show_memory_flags(edit['channel'])
        self.memory_flag_edit = None
        self.memory_select_read = None
        self.memory_flag_timer.Stop()
        if edit is not None and edit['restore'] is not None and not edit.get('restored') and self.serial.is_open:
            try:
                if self.write_serial(edit['restore']) != len(edit['restore']):
                    message += ' Receiver restoration failed.'
            finally:
                self.release_monitor_mute()
                self.write_serial(b'RX\r\n')
        else:
            self.release_monitor_mute()
        self.aor_status.SetStatusText(message)
        self.update_connection_ui()
        if self.memory_channel_pending is not None and self.memory_channel_pending['phase'] == 'preserve':
            if verified and edit is not None and self.memory_rows[edit['channel']][edit['flag']] == edit['desired']:
                wx.CallAfter(self.continue_memory_channel_edit)
            else:
                self.finish_memory_channel_edit('Channel data read back, but original flags could not be restored: ' + message)

    def on_memory_flag_timeout(self, event):
        for channel, entry in self.memory_rows.items():
            if self.memory_select_read is not None:
                entry['select'] = None
                self.show_memory_flags(channel)
        self.finish_memory_flag_edit('Memory flag read-back failed or timed out; state was not assumed. Refresh to retry.')

    def prepare_list(self, columns):
        self.edit_list.show_memory(False)
        self.edit_list.list.ClearAll()
        for index, (label, width) in enumerate(columns):
            self.edit_list.list.InsertColumn(index, label, width=width)

    def get_select_scan(self):
        self.select_scan_rows.clear()
        self.prepare_list((('Slot', 45), ('Channel', 65), ('Frequency', 140),
                           ('Step', 100), ('Auto', 45), ('Mode', 55),
                           ('ATT', 40), ('Name', 130)))
        self.aor_status.SetStatusText('Selected Channels: Refresh reads membership; Stored Channels / Selected Channels starts it.')
        if self.serial.is_open:
            self.write_serial(b'GR\r\n')

    def set_select_scan(self, text):
        entry = parse_select_scan_response(text)
        if self.memory_select_read is not None:
            self.memory_select_read['seen'] = True
            slot = int(entry['slot'])
            slots = self.memory_select_read['slots']
            if slot in slots and slots[slot] != entry['channel']:
                self.memory_select_read['invalid'] = True
            slots[slot] = entry['channel']
            if entry['channel'] is not None:
                self.memory_select_read['channels'].add(entry['channel'])
        if self.current_list_view() != 'SELECT SCAN':
            return
        if entry['channel'] is None:
            self.aor_status.SetStatusText('Selected Channels: %d tagged channels' % len(self.select_scan_rows))
            return
        slot = entry['slot']
        if slot not in self.select_scan_rows:
            self.select_scan_rows[slot] = self.edit_list.list.InsertItem(
                self.edit_list.list.GetItemCount(), slot)
        values = (slot, entry['channel'], display_frequency(entry['frequency_hz']),
                  display_step(entry['step_khz'] * 1000), entry['auto'],
                  self.cbx_mode.GetString(MD_TO_GUI_MODE[entry['mode']]),
                  entry['attenuation'], entry['name'])
        self.edit_list.list.fill_line(self.select_scan_rows[slot], values)

    def get_pass_frequencies(self):
        self.pass_frequency_rows.clear()
        self.prepare_list((('Slot', 55), ('Context', 115), ('Frequency', 140), ('State', 80)))
        self.aor_status.SetStatusText('Pass frequencies: %s. Refresh chooses bank / VFO; right-click removes bank entries' %
                                     self.pass_context)
        if self.serial.is_open:
            self.write_serial(('PR%s\r\n' % self.pass_context).encode('ascii'))

    def set_pass_frequency(self, text):
        entry = parse_pass_frequency_response(text)
        if self.current_list_view() != 'PASS FREQS' or entry['context'] != self.pass_context:
            return
        slot = entry['slot']
        previous = self.pass_frequency_rows.get(slot)
        row = previous['row'] if previous is not None else self.edit_list.list.InsertItem(
            self.edit_list.list.GetItemCount(), slot)
        entry['row'] = row
        self.pass_frequency_rows[slot] = entry
        frequency = entry['frequency_hz']
        self.edit_list.list.fill_line(row, (slot, 'VFO' if self.pass_context == 'V' else 'Bank %s' % self.pass_context,
                                           '---' if frequency is None else display_frequency(frequency),
                                           'Empty' if frequency is None else 'Pass'))

    def on_choose_pass_context(self, evt):
        contexts = ['V'] + list('ABCDEFGHIJKLMNOPQRSTabcdefghijklmnopqrst')
        labels = ['VFO (V)'] + ['Search bank %s' % context for context in contexts[1:]]
        dialog = wx.SingleChoiceDialog(self, 'Read pass frequencies for:', 'Pass frequencies', labels)
        dialog.SetSelection(contexts.index(self.pass_context))
        try:
            if dialog.ShowModal() == wx.ID_OK:
                self.pass_context = contexts[dialog.GetSelection()]
                self.get_pass_frequencies()
        finally:
            dialog.Destroy()

    def on_add_pass_frequency(self, evt):
        if self.current_list_view() != 'PASS FREQS' or not self.connected or not self.serial.is_open:
            return
        dialog = wx.TextEntryDialog(self, 'Add frequency in MHz to pass list %s:' % self.pass_context,
                                    'Add pass frequency')
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            try:
                command = make_pass_frequency_command(self.pass_context, dialog.GetValue().strip())
            except ValueError as error:
                wx.MessageBox(str(error), 'Invalid pass frequency', wx.OK | wx.ICON_ERROR, self)
                return
            if self.write_serial(command) == len(command):
                self.get_pass_frequencies()
        finally:
            dialog.Destroy()

    def remove_pass_frequency(self, slot):
        entry = self.pass_frequency_rows.get(slot)
        if (self.current_list_view() != 'PASS FREQS' or not self.connected or not self.serial.is_open
                or entry is None or entry['frequency_hz'] is None or entry['context'] == 'V'):
            return
        command = ('PD%s%s\r\n' % (entry['context'], slot)).encode('ascii')
        if self.write_serial(command) == len(command):
            self.get_pass_frequencies()

    def show_log_view(self):
        self.prepare_list((('Time', 180), ('Frequency', 140), ('Source', 100),
                           ('Level', 50), ('State', 70), ('Duration', 80)))
        for activity in self.activity_events:
            row = self.edit_list.list.InsertItem(self.edit_list.list.GetItemCount(), '')
            self.edit_list.list.fill_line(row, format_activity_row(activity))
        self.aor_status.SetStatusText('Activity log: %d / 200 events (read-only)' % len(self.activity_events))

    def get_search_banks(self):
        """
        SRx<CR> where x = A-T or a-t  -> Recalls search bank x
        SR%%<CR>                      -> Responds with a listing of all search banks A-J
        Responds with:
        SRx SLnnnnnnnnnn SUnnnnnnnnnn STnnnnnn AUn MDn TTxxx...x
        """
        self.filling_banks = False
        self.pending_memory_channels.clear()
        self.row = 0
        self.search_bank_rows.clear()
        self.edit_list.show_memory(False)
        self.edit_list.list.ClearAll()
        # set column names
        column_headers = (('Stored Range', 85), ('Lower', 125), ('Upper', 125), ('Step', 100),
                          ('Auto', 45), ('Mode', 55), ('Att', 40), ('Name', 130))
        for column, (label, width) in enumerate(column_headers):
            self.edit_list.list.InsertColumn(column, label, width=width)

        towrite = []
        comm = 'SR%%\r\n'
        channels = ['K', 'L', 'M', 'N', 'O', 'P', 'Q', 'R', 'S', 'T',
                    'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o', 'p', 'q', 'r', 's', 't']
        towrite.append(comm)
        for item in channels:
            comm = 'SR%s\r\n' % item
            towrite.append(comm)
        self.write_serial(''.join(towrite).encode("ascii"))

    def parse_memory_bank_line(self, text):
        match = re.fullmatch(r'MW ([A-Ta-t]):([0-9]+) TB\1(.*)', text)
        if match is None:
            raise ValueError('Invalid bank-list response')
        bank, channels, name = match.groups()
        count = int(channels)
        if bank not in 'ABCDEFGHIJabcdefghij' or not 0 <= count <= 100:
            raise ValueError('Invalid memory bank identifier/count')
        return {'bank': bank, 'channels': count, 'name': name.strip()}

    def parse_memory_line(self, text):
        empty = re.fullmatch(r'MX([A-Ta-t][0-9]{2}) ---', text)
        if empty is not None:
            return empty.group(1), None
        fields = text.split(None, 7)
        self.validate_fields(fields, ('MX', 'MP', 'RF', 'ST', 'AU', 'MD', 'AT', 'TM'))
        values = [field[2:] for field in fields]
        return values[0], values

    def set_memory_banks_list(self, text):
        """"""
        metadata = self.parse_memory_bank_line(text)
        bank = metadata['bank']
        previous = self.selected_memory_bank()
        previous_index = self.cbx_lists.GetSelection()
        self.memory_banks[bank] = metadata
        self.cbx_lists.Clear()
        for view in ('SEARCH BANKS', 'SELECT SCAN', 'PASS FREQS', 'LOG VIEW', 'DATABASE'):
            self.cbx_lists.Append(LIST_LABELS[view])
        for identifier in sorted(self.memory_banks, key=lambda b: (b.upper(), b.islower())):
            metadata = self.memory_banks[identifier]
            label = metadata['name'] if metadata['name'] and metadata['name'] != identifier else '%s:%d' % (identifier, metadata['channels'])
            index = self.cbx_lists.Append(label, metadata)
            if previous and previous['bank'] == identifier:
                self.cbx_lists.SetSelection(index)
        if previous is None:
            self.cbx_lists.SetSelection(previous_index if 0 <= previous_index < 5 else wx.NOT_FOUND)
        self.sync_memory_choices()
        self.maybe_load_initial_bank()

    def set_memory_banks(self, item):
        """initializes and fills memory Bank ListControl
        """
        channel, columns = self.parse_memory_line(item)
        if columns is None:
            if self.selected_memory_bank() is not None and channel[0] == self.selected_memory_bank()['bank']:
                self.memory_empty_channels.add(channel)
            self.pending_memory_channels.discard(channel)
            self.filling_banks = bool(self.pending_memory_channels)
            if not self.filling_banks:
                self.read_memory_select_flags()
            return
        if columns[0] not in self.pending_memory_channels:
            return
        if columns[0] == self.last:
            return
        row = len(self.edit_list.memory_model.channels)
        self.memory_rows[columns[0]] = {'row': row, 'fields': columns, 'select': None, 'skip': columns[1] == '1'}
        self.edit_list.memory_model.append(columns[0])
        self.show_memory_flags(columns[0])

        self.last = columns[0]
        self.row += 1
        self.pending_memory_channels.remove(columns[0])
        self.filling_banks = bool(self.pending_memory_channels)
        if not self.filling_banks:
            self.read_memory_select_flags()

    def update_search_bank_choices(self):
        choice = self.search_bank_choice
        selected = choice.GetSelection()
        selected_bank = choice.GetClientData(selected) if selected != wx.NOT_FOUND else None
        names = {}
        counts = {}
        for bank, entry in self.search_banks.items():
            name = entry.get('name', '').strip()
            if name and name != bank:
                names[bank] = name
                counts[name] = counts.get(name, 0) + 1
        selected_index = wx.NOT_FOUND
        for index in range(choice.GetCount()):
            bank = choice.GetClientData(index)
            name = names.get(bank)
            label = bank if name is None else (name + ' [%s]' % bank if counts[name] > 1 else name)
            choice.SetString(index, label)
            if bank == selected_bank:
                selected_index = index
        choice.SetSelection(selected_index)

    def set_search_banks(self, item):
        entry = parse_search_bank_response(item)
        bank = entry['bank']
        self.search_banks[bank] = entry
        self.update_search_bank_choices()
        if self.current_list_view() == 'SEARCH BANKS':
            if bank not in self.search_bank_rows:
                self.search_bank_rows[bank] = self.edit_list.list.InsertItem(self.edit_list.list.GetItemCount(), bank)
            if entry['empty']:
                values = (bank, '---', '---', '---', '---', '---', '---', '')
            else:
                values = (bank, display_frequency(entry['lower_hz']), display_frequency(entry['upper_hz']),
                          display_step(entry['step_khz'] * 1000), str(int(entry['auto'])),
                          self.cbx_mode.GetString(MD_TO_GUI_MODE[entry['mode']]), entry['attenuation'] or '---', entry['name'])
            self.edit_list.list.fill_line(self.search_bank_rows[bank], values)
        if bank in (self.edit_bank_pending, self.bank_write_pending):
            self.edit_bank_pending = None
            self.bank_write_pending = None
            if self.search_bank_dialog is not None:
                self.search_bank_dialog.show_bank(entry)
            self.aor_status.SetStatusText('Search Bank %s read back from scanner' % bank)

    def set_vfo_text(self, text, vfx, active=True):
        data = text.split()[1:]
        self.validate_fields(data, ('RF', 'ST', 'AU', 'MD', 'AT'))
        freq, step, auto, mode, att = (item[2:] for item in data)
        if vfx == 0:
            lb = self.lb_vfa
            self.vfa = freq
        elif vfx == 1:
            lb = self.lb_vfb
            self.vfb = freq
        elif vfx == 2:
            lb = self.lb_vfo
            self.vfo = freq
        else:
            return

        lb.SetLabel(format_frequency(freq))
        self.vfo_status[vfx] = text
        if not active:
            return
        self.rb_vfos.SetSelection(vfx)
        # step
        if '.' in step:
            pass
            # in megaherz
        else:
            # in kilos
            step = float(step) / 1000
            values = self.cbx_step.GetItems()
            for index, value in enumerate(values):
                if float(value) == step:
                    self.cbx_step.SetSelection(index)
                    break
            else:
                self.cbx_step.SetValue('%g' % step)
        # auto
        self.ckbx_auto.SetValue(int(auto))
        # mode
        self.cbx_mode.SetSelection(MD_TO_GUI_MODE[int(mode)])
        # att
        self.ckbx_att.SetValue(int(att))
        if not self.monitor_muted:
            self.afc_mode = int(mode)
            self.update_receiver_switch_ui()

        self.lb_vfo.Hide()
        self.lb_vfo.Show()

    def com_thread(self):
        """Thread that handles the incoming traffic. Does the basic input
           transformation (newlines) and generates an SerialRxEvent"""
        while self.alive.is_set():
            try:
                textline = self.serial.read_until(b'\n')
            except serial.SerialException as error:
                self.alive.clear()
                print('Serial read error: %s' % error, file=sys.stderr)
                if self.memory_inventory is not None:
                    wx.CallAfter(self.finish_memory_inventory, 'Export failed: serial read error: %s' % error)
                elif self.memory_channel_pending is not None and self.memory_flag_edit is None:
                    wx.CallAfter(self.finish_memory_channel_edit, 'Serial read failed; channel state was not verified.')
                elif self.monitor_muted:
                    wx.CallAfter(self.finish_memory_flag_edit, 'Serial read failed; restoring receiver/audio.')
                return
            if not self.alive.is_set():
                return
            if not textline.strip():
                continue
            if not textline.endswith(b'\n'):
                print('Ignored partial scanner response: %r' % textline, file=sys.stderr)
                continue
            try:
                textline = textline.decode("ascii")
            except UnicodeDecodeError as error:
                print('Ignored non-ASCII scanner response: %s' % error, file=sys.stderr)
                continue
            if textline == '?\r\n':
                continue
            textline = textline.rstrip('\r\n')
            if not textline.startswith('LM'):
                textline = textline.strip()
            if textline:
                if DEBUG_SERIAL:
                    print('Serial RX: %r' % textline, file=sys.stderr)
                event = SerialRxEvent(self.GetId(), [textline])
                self.GetEventHandler().AddPendingEvent(event)


class MyApp(wx.App):
    # noinspection PyPep8Naming
    def OnInit(self):
        wx.InitAllImageHandlers()
        frame_terminal = AorCtrl(None, -1, "")
        self.SetTopWindow(frame_terminal)
        frame_terminal.Show(1)
        return 1


if __name__ == "__main__":
    app = MyApp(0)
    app.MainLoop()
