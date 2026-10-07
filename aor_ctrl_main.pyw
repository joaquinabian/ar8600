import wx
import sys
import serial_conf_dialog
import serial
import threading
import re
import time
from collections import deque
from datetime import datetime
from aor_control_frame import AorCtrlFrame
from aor_functions import (do_nothing, format_frequency, parse_lm_response, parse_lc_response,
                           parse_select_scan_response, parse_pass_frequency_response,
                           make_pass_frequency_command, format_activity_row,
                           parse_bandscope_status, BandscopeSweep, bandscope_frequency)


# GUI order: WFM, NFM, SFM, WAM, AM, NAM, USB, LSB, CW.
GUI_MODE_TO_MD = (0, 1, 6, 7, 2, 8, 3, 4, 5)
MD_TO_GUI_MODE = {code: index for index, code in enumerate(GUI_MODE_TO_MD)}


menu_titles = ["Set", "Edit"]
menu_title_by_id = {}
for title_ in menu_titles:
    menu_title_by_id[wx.NewId()] = title_


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
        self.sweep = None
        self.waiting = None
        self.running = False
        self.entered = False
        self.restore_command = None
        self.close_requested = False
        self.started_at = None
        self.last_duration = None
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.start_button = wx.Button(panel, label='Start')
        self.stop_button = wx.Button(panel, label='Stop')
        self.refresh_button = wx.Button(panel, label='Refresh once')
        for button in (self.start_button, self.stop_button, self.refresh_button):
            buttons.Add(button, 0, wx.ALL, 5)
        sizer.Add(buttons)
        self.info = wx.StaticText(panel, label='Centre: ---    Span: ---    Marker: ---')
        sizer.Add(self.info, 0, wx.ALL, 5)
        self.plot = wx.Panel(panel)
        self.plot.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.plot.Bind(wx.EVT_PAINT, self.on_paint)
        self.plot.Bind(wx.EVT_SIZE, self.on_plot_size)
        sizer.Add(self.plot, 1, wx.EXPAND | wx.ALL, 5)
        self.message = wx.StaticText(panel, label='Connect, then Start or Refresh once. Stop halts refresh; Close restores the previous VFO/memory mode when available.')
        sizer.Add(self.message, 0, wx.ALL, 5)
        panel.SetSizer(sizer)
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_timer, self.timer)
        self.start_button.Bind(wx.EVT_BUTTON, self.on_start)
        self.stop_button.Bind(wx.EVT_BUTTON, self.on_stop)
        self.refresh_button.Bind(wx.EVT_BUTTON, self.on_refresh)
        self.Bind(wx.EVT_CLOSE, self.on_close)

    @property
    def pauses_lm(self):
        return self.waiting is not None and (self.sweep is None or not self.sweep.failed)

    def available(self):
        return self.controller.connected and self.controller.serial.is_open and self.controller.alive.is_set()

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
        self.request_sweep()

    def on_refresh(self, event):
        if self.waiting is None:
            self.running = False
            self.request_sweep()

    def on_stop(self, event):
        self.running = False
        if self.waiting is None:
            self.timer.Stop()
        self.message.SetLabel('Stopping after the pending sweep.' if self.waiting else 'Refresh stopped.')

    def request_sweep(self):
        if not self.available():
            self.running = False
            self.message.SetLabel('Connect to the scanner before requesting a sweep.')
            return
        if self.waiting is not None:
            return
        self.close_requested = False
        self.sweep = None
        self.waiting = 'status' if self.entered else 'receiver'
        self.start_button.Disable()
        self.refresh_button.Disable()
        self.started_at = time.monotonic()
        # One-shot watchdog reports failure; it never sends another DS request.
        self.timer.StartOnce(75000)
        self.message.SetLabel('Reading bandscope status (LM polling paused).')
        self.send(b'AM\r\n' if self.entered else b'RX\r\n')

    def capture_receiver(self, text):
        if self.waiting != 'receiver':
            return
        context = text.split()[0]
        if context in ('VA', 'VB', 'VF'):
            self.restore_command = (context + '\r\n').encode('ascii')
        elif context == 'MR':
            match = re.match(r'MR MX([A-Ja-j][0-9]{2}) ', text)
            if match:
                self.restore_command = ('MR%s\r\n' % match.group(1)).encode('ascii')
        elif context not in ('SM', 'MS') and not re.fullmatch(r'SR[A-Ta-t]', context):
            return
        self.waiting = 'status'
        self.entered = True
        # The first AM enters; the second obtains the documented status.
        self.send(b'AM\r\nAM\r\n')

    def set_status(self, text):
        status = parse_bandscope_status(text)
        if self.waiting == 'receiver':  # The scanner was already in analyser mode.
            self.entered = True
            self.waiting = 'status'
        if self.waiting != 'status':
            return
        if self.status != status:
            self.samples = None  # Never relabel an old trace with new geometry.
            self.plot.Refresh()
        self.status = status
        self.info.SetLabel('Centre: %.6f MHz    Span: %g MHz    Marker: %.6f MHz%s' %
                           (status['centre_hz'] / 1000000, status['span_hz'] / 1000000,
                            status['marker_hz'] / 1000000, '    Peak hold' if status['peak_hold'] else ''))
        self.sweep = BandscopeSweep()
        self.waiting = 'data'
        self.started_at = time.monotonic()
        self.timer.StartOnce(75000)
        self.message.SetLabel('Waiting for sweep completion: 0 / 1024 samples (LM polling paused).')
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
            self.last_duration = time.monotonic() - self.started_at
            self.message.SetLabel('1024 samples received in %.2f s. 0: unmeasured; 1: outside specification; 2-F: raw level.' % self.last_duration)
            self.plot.Refresh()
        elif not self.sweep.failed:
            self.message.SetLabel('Collecting: %d / 1024 samples (LM polling paused).' % (len(self.sweep.blocks) * 32))
        if self.sweep.finished:
            if self.sweep.failed:
                self.message.SetLabel('Sweep rejected: incomplete or malformed data. Refresh once to retry.')
            self.waiting = None
            self.timer.Stop()
            self.start_button.Enable()
            self.refresh_button.Enable()
            if self.close_requested:
                self.restore_receiver()
            elif self.running:
                self.timer.StartOnce(500)

    def on_timer(self, event):
        if self.waiting is None:
            if self.running:
                self.request_sweep()
            return
        self.running = False
        if self.waiting == 'data':
            self.sweep.failed = True
            self.message.SetLabel('Sweep timed out; LM resumed. Waiting for DS end; reconnect if it never arrives.')
            # Retain the outstanding request to prevent overlap with late data.
        else:
            self.waiting = None
            self.start_button.Enable()
            self.refresh_button.Enable()
            self.message.SetLabel('No valid bandscope status received. Refresh once to retry.')
            if self.close_requested:
                self.restore_receiver()
        print('Bandscope request timed out', file=sys.stderr)

    def restore_receiver(self, query=True):
        if self.entered and self.restore_command and self.controller.serial.is_open:
            self.controller.write_serial(self.restore_command + (b'RX\r\n' if query else b''))
        elif self.entered and self.controller.serial.is_open:
            self.controller.aor_status.SetStatusText('Bandscope refresh stopped. Select a VFO to leave analyser mode.')
        self.entered = False
        self.restore_command = None

    def disconnect(self):
        self.running = False
        self.timer.Stop()
        self.restore_receiver(query=False)
        self.waiting = None
        self.sweep = None
        self.start_button.Enable()
        self.refresh_button.Enable()
        self.message.SetLabel('Disconnected.')

    def on_close(self, event):
        self.on_stop(event)
        self.close_requested = True
        if self.waiting != 'data':
            self.waiting = None
            self.timer.Stop()
            self.start_button.Enable()
            self.refresh_button.Enable()
        if self.waiting is None:
            self.restore_receiver()
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
            dc.DrawText('Marker', marker_x + 4, top)
        if status['centre_hz'] != status['marker_hz']:
            dc.DrawText('Marker differs from centre: DS frequency geometry is not established by the manual.', left + 5, top + 30)
            return
        if self.samples is None:
            return
        dc.SetPen(wx.Pen('#1565b0', 1))
        previous = None
        for index, level in enumerate(self.samples):
            frequency = bandscope_frequency(status, index)
            if not low <= frequency <= high or level < 2:
                previous = None  # 0/1 are validity codes, not measured signal levels.
                continue
            point = (x_at(frequency), bottom - round(level * (bottom - top) / 15))
            if previous is None:
                dc.DrawPoint(*point)
            else:
                dc.DrawLine(*previous, *point)
            previous = point


# noinspection PyUnusedLocal
class AorCtrl(AorCtrlFrame):
    """Simple terminal program for wxPython"""

    def __init__(self, *args, **kwds):
        self.serial = serial.Serial(baudrate=9600, bytesize=8, stopbits=2,
                                    parity='N', rtscts=0, xonxoff=1)

        self.serial.port = 'COM7'      # if serial is instantiated with port parameter, then it is opened
        self.serial.timeout = 1        # make sure that the alive event can be checked from time to time
        self.thread = None
        self.row = 0
        self.last = None
        self.vfa = None
        self.vfb = None
        self.vfo = None
        self.vfo_status = {}
        self.background_vfo = None
        self.memory_banks = []
        self.connected = False
        self.filling_banks = False
        self.pending_memory_channels = set()
        self.list_item_clicked = None
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

        self.create_monitor_controls()
        self.lm_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_lm_timer, self.lm_timer)
        self.__set_properties()
        self.__attach_events()           # register events
        view_menu = wx.Menu()
        bandscope_action = view_menu.Append(wx.ID_ANY, 'BAND SCOPE...')
        self.GetMenuBar().Append(view_menu, 'View')
        self.Bind(wx.EVT_MENU, self.on_bandscope, id=bandscope_action.GetId())

    def on_bandscope(self, event):
        if self.bandscope is None:
            self.bandscope = BandscopeWindow(self)
        self.bandscope.close_requested = False
        self.bandscope.Show()
        self.bandscope.Raise()

    def start_thread(self):
        """Start the receiver thread"""
        self.thread = threading.Thread(target=self.com_thread)
        self.thread.daemon = True
        self.alive.set()
        self.thread.start()

    def stop_thread(self):
        """Stop the receiver thread, wait util it's finished."""
        if self.bandscope is not None:
            self.bandscope.disconnect()
        self.stop_monitoring()
        self.connected = False
        self.filling_banks = False
        self.pending_memory_channels.clear()
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
        for index, (label, width) in enumerate((('Time', 155), ('Frequency MHz', 120),
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
            return
        if self.lm_pending:
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
        if len(self.activity_events) == self.activity_events.maxlen:
            self.activity_list.DeleteItem(0)
        self.activity_events.append(activity)
        row = self.activity_list.InsertItem(self.activity_list.GetItemCount(),
                                            activity['timestamp'].strftime('%H:%M:%S.%f')[:-3])
        frequency = activity['frequency_hz']
        values = ('---' if frequency is None else format(frequency / 1000000, '.6f'),
                  '%s %s' % (activity['source'], activity['source_id']),
                  str(activity['level']),
                  '---' if activity['duration'] is None else '%.2f s' % activity['duration'],
                  'OPEN' if activity['squelch_open'] else 'CLOSED')
        for column, value in enumerate(values, 1):
            self.activity_list.SetItem(row, column, value)
        self.activity_list.EnsureVisible(row)
        if self.cbx_lists.GetStringSelection() == 'LOG VIEW':
            self.show_log_view()

    def __set_properties(self):
        self.SetTitle("Serial Terminal")
        # self.SetSize((546, 383))

    def __attach_events(self):
        # register events at the controls
        self.Bind(wx.EVT_TOOL, self.on_tool)
        self.cbx_lists.Bind(wx.EVT_MOUSEWHEEL, do_nothing)
        self.Bind(wx.EVT_LIST_ITEM_RIGHT_CLICK, self.open_menu, self.edit_list.list)
        self.tuning_panel.tune_arrows.Bind(wx.EVT_BUTTON, self.on_move_frequency)
        self.Bind(wx.EVT_BUTTON, self.on_enter_freq, self.tuning_panel.tune_benter)
        self.Bind(wx.EVT_COMBOBOX, self.on_select_list, self.cbx_lists)
        self.Bind(wx.EVT_BUTTON, self.on_select_list, self.bt_refresh)
        self.Bind(wx.EVT_CHECKBOX, self.on_auto, self.ckbx_auto)
        self.Bind(wx.EVT_CHECKBOX, self.on_enter_att, self.ckbx_att)
        self.Bind(wx.EVT_COMBOBOX, self.on_select_mode, self.cbx_mode)
        self.Bind(wx.EVT_COMBOBOX, self.on_select_step, self.cbx_step)
        self.Bind(wx.EVT_RADIOBOX, self.on_select_vfo, self.rb_vfos)
        self.Bind(wx.EVT_BUTTON, self.on_vfo_start, self.bt_vfostart)
        self.Bind(wx.EVT_BUTTON, self.on_vfo_stop, self.bt_vfostop)
        self.Bind(wx.EVT_BUTTON, self.on_select_scan_start, self.bt_start)
        self.Bind(wx.EVT_BUTTON, self.on_add_pass_frequency, self.bt_mkpassfreq)
        self.Bind(EVT_SERIALRX, self.on_serial_read)
        self.Bind(wx.EVT_CLOSE, self.on_close)

    def on_tool(self, evt):
        tb = self.GetToolBar()
        item = tb.FindById(evt.GetId())
        if item.Label == 'config':
            self.set_port()
        elif item.Label == 'connect':
            self.connect()
        else:
            print('some other tool pressed')

    def open_menu(self, event):
        """"""
        view = self.cbx_lists.GetStringSelection()
        if view in ('LOG VIEW', 'SELECT SCAN'):
            return
        if view == 'PASS FREQS':
            row = event.GetIndex()
            slot = self.edit_list.list.GetItemText(row)
            entry = self.pass_frequency_rows.get(slot)
            menu = wx.Menu()
            choose = menu.Append(wx.ID_ANY, 'Choose pass-frequency bank / VFO...')
            menu.Bind(wx.EVT_MENU, self.on_choose_pass_context, id=choose.GetId())
            remove = menu.Append(wx.ID_ANY, 'Remove pass frequency')
            remove.Enable(entry is not None and entry['frequency_hz'] is not None
                          and entry['context'] != 'V')
            menu.Bind(wx.EVT_MENU, lambda evt: self.remove_pass_frequency(slot),
                      id=remove.GetId())
            self.edit_list.list.PopupMenu(menu)
            menu.Destroy()
            return
        self.list_item_clicked = event.GetText()
        x, y = event.GetPoint()

        menu = wx.Menu()
        for (id_, title) in menu_title_by_id.items():
            menu.Append(id_, title)
            menu.Bind(wx.EVT_MENU, self.menu_selection_cb, id=id_)
        self.edit_list.list.PopupMenu(menu, (x+10, y))
        menu.Destroy()

    def menu_selection_cb(self, event):
        # do something
        operation = menu_title_by_id[event.GetId()]
        target = self.list_item_clicked
        print('Perform "%s" on "%s."' % (operation, target))

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

        self.write_serial(func.encode("ascii"))
        self.write_serial('RX\r\n'.encode("ascii"))

    def on_auto(self, evt):
        if self.ckbx_auto.IsChecked():
            self.write_serial('AU1\r\nRX\r\n'.encode("ascii"))
        else:
            self.write_serial('AU0\r\n'.encode("ascii"))

    def on_enter_att(self, evt):
        """Changes Attenuation ON/OFF
        """
        att = 1 if self.ckbx_att.IsChecked() else 0
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
        # print 'towrite ', towrite
        self.write_serial(towrite.encode("ascii"))

    def on_vfo_start(self, evt):
        self.write_serial('VS\r\n'.encode("ascii"))

    def on_vfo_stop(self, evt):
        self.write_serial('VV0\r\n'.encode("ascii"))

    def on_select_scan_start(self, evt):
        if self.cbx_lists.GetStringSelection() == 'SELECT SCAN' and self.connected and self.serial.is_open:
            self.write_serial(b'SM\r\n')
            self.aor_status.SetStatusText('Select Scan start requested')

    def on_select_mode(self, evt):
        """Set mode on RX
        """
        mode = self.cbx_mode.GetSelection()
        if not 0 <= mode < len(GUI_MODE_TO_MD):
            return
        self.write_serial(('MD%s\r\n' % GUI_MODE_TO_MD[mode]).encode("ascii"))

    def on_select_step(self, evt):
        """Set step on RX
        STnnnnm0<CR> Set the tuning step size in Hz
        STnnn.nm<CR> Set the tuning step size in kHz
        """
        step = float(self.cbx_step.GetStringSelection())
        self.write_serial(('ST%06.2f\r\n' % step).encode("ascii"))

    def on_enter_freq(self, evt):
        """Writes command RF to serial
        """
        freq = self.tuning_panel.freq
        if not 0.1 < freq < 3000:
            return
        comm = 'RF%010.5f\r\n' % freq
        self.write_serial(comm.encode("ascii"))

    def on_select_list(self, evt):
        selection = self.cbx_lists.GetStringSelection()
        self.filling_banks = False
        self.pending_memory_channels.clear()
        print(selection)
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
                if self.bandscope is not None:
                    self.bandscope.capture_receiver(first)
                # print 'event text ', text
                if first.startswith('LM'):
                    self.set_signal_level(first)
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
                elif first.startswith('SR'):
                    if self.cbx_lists.GetStringSelection() not in ('SELECT SCAN', 'PASS FREQS', 'LOG VIEW'):
                        self.set_search_banks(first)
                elif first.startswith('GR'):
                    self.set_select_scan(first)
                elif first.startswith('PR'):
                    self.set_pass_frequency(first)
                elif first.startswith('SM '):
                    fields = first.split(None, 8)[1:]
                    self.validate_fields(fields, ('MX', 'MP', 'RF', 'ST', 'AU', 'MD', 'AT', 'TM'))
                    self.aor_status.SetStatusText('Select Scan: %s, %s MHz' %
                                                (fields[0][2:], format_frequency(fields[2][2:])))
                elif first.startswith('MW'):
                    self.set_memory_banks_list(first)
                elif first.startswith('MX'):
                    if self.filling_banks:
                        self.set_memory_banks(first)
                    else:
                        continue
                elif first.startswith('MR '):
                    # RX memory status is not an MA listing response.
                    continue
                else:
                    print('Ignored unexpected scanner response: %r' % first, file=sys.stderr)
                    continue

                received_valid = True
            except (ValueError, IndexError, TypeError) as error:
                print('Ignored malformed scanner response %r: %s' % (first, error), file=sys.stderr)
        if received_valid and self.serial.is_open and self.alive.is_set() and not self.connected:
            self.connected = True
            self.start_monitoring()

    def write_serial(self, data):
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
        print("log_new")

    def log_open(self, event):  # wxGlade: AorCtrlFrame.<event_handler>
        print("log_open")

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
            self.memory_banks = []
            self.write_serial('RX\r\n'.encode("ascii"))
            self.write_serial('TB\r\n'.encode("ascii"))
            self.write_serial('TB\r\n'.encode("ascii"))

    def get_memory_banks(self):
        """
        MA[bank] starts at channels 00-09; bare MA advances ten channels.
        Populated channels return MX[bank][channel] MP RF ST AU MD AT TM.
        Empty channels return MX[bank][channel] ---.
        """
        selection = self.cbx_lists.GetStringSelection()
        bank, channels = selection.split()[0].split(':')
        self.row = 0
        self.last = None
        self.edit_list.list.ClearAll()
        # set column names
        column_headers = ['Channel', 'PASS', 'FREQ', 'ST', 'AUTO', 'MODE', 'ATT', 'NAME']
        for column, label in enumerate(column_headers):
            self.edit_list.list.InsertColumn(column, label)

        channel_count = int(channels)
        blocks = (channel_count + 9) // 10
        self.pending_memory_channels = {'%s%02i' % (bank, channel) for channel in range(channel_count)}
        self.filling_banks = bool(self.pending_memory_channels)
        if blocks:
            towrite = 'MA%s\r\n' % bank + 'MA\r\n' * (blocks - 1)
            self.write_serial(towrite.encode("ascii"))

    def prepare_list(self, columns):
        self.edit_list.list.ClearAll()
        for index, (label, width) in enumerate(columns):
            self.edit_list.list.InsertColumn(index, label, width=width)

    def get_select_scan(self):
        self.select_scan_rows.clear()
        self.prepare_list((('Slot', 45), ('Channel', 65), ('Frequency MHz', 115),
                           ('Step kHz', 70), ('Auto', 45), ('Mode', 55),
                           ('ATT', 40), ('Name', 130)))
        self.aor_status.SetStatusText('Select Scan: Refresh reads entries; Scan Start starts Select Scan')
        if self.serial.is_open:
            self.write_serial(b'GR\r\n')

    def set_select_scan(self, text):
        entry = parse_select_scan_response(text)
        if self.cbx_lists.GetStringSelection() != 'SELECT SCAN':
            return
        if entry['channel'] is None:
            self.aor_status.SetStatusText('Select Scan: %d tagged channels' % len(self.select_scan_rows))
            return
        slot = entry['slot']
        if slot not in self.select_scan_rows:
            self.select_scan_rows[slot] = self.edit_list.list.InsertItem(
                self.edit_list.list.GetItemCount(), slot)
        values = (slot, entry['channel'], format(entry['frequency_hz'] / 1000000, '.6f'),
                  format(entry['step_khz'].normalize(), 'f'), entry['auto'],
                  self.cbx_mode.GetString(MD_TO_GUI_MODE[entry['mode']]),
                  entry['attenuation'], entry['name'])
        self.edit_list.list.fill_line(self.select_scan_rows[slot], values)

    def get_pass_frequencies(self):
        self.pass_frequency_rows.clear()
        self.prepare_list((('Slot', 55), ('Context', 115), ('Frequency MHz', 140), ('State', 80)))
        self.aor_status.SetStatusText('Pass frequencies: %s. Refresh chooses bank / VFO; right-click removes bank entries' %
                                     self.pass_context)
        if self.serial.is_open:
            self.write_serial(('PR%s\r\n' % self.pass_context).encode('ascii'))

    def set_pass_frequency(self, text):
        entry = parse_pass_frequency_response(text)
        if self.cbx_lists.GetStringSelection() != 'PASS FREQS' or entry['context'] != self.pass_context:
            return
        slot = entry['slot']
        previous = self.pass_frequency_rows.get(slot)
        row = previous['row'] if previous is not None else self.edit_list.list.InsertItem(
            self.edit_list.list.GetItemCount(), slot)
        entry['row'] = row
        self.pass_frequency_rows[slot] = entry
        frequency = entry['frequency_hz']
        self.edit_list.list.fill_line(row, (slot, 'VFO' if self.pass_context == 'V' else 'Bank %s' % self.pass_context,
                                           '---' if frequency is None else format(frequency / 1000000, '.6f'),
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
        if self.cbx_lists.GetStringSelection() != 'PASS FREQS' or not self.connected or not self.serial.is_open:
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
        if (self.cbx_lists.GetStringSelection() != 'PASS FREQS' or not self.connected or not self.serial.is_open
                or entry is None or entry['frequency_hz'] is None or entry['context'] == 'V'):
            return
        command = ('PD%s%s\r\n' % (entry['context'], slot)).encode('ascii')
        if self.write_serial(command) == len(command):
            self.get_pass_frequencies()

    def show_log_view(self):
        self.prepare_list((('Time', 180), ('Frequency MHz', 120), ('Source', 100),
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
        self.edit_list.list.ClearAll()
        # set column names
        column_headers = ['Bank', 'Start', 'Stop', 'Step', 'AUTO', 'MODE', 'ATT', 'NAME']
        for column, label in enumerate(column_headers):
            self.edit_list.list.InsertColumn(column, label)

        towrite = []
        comm = 'SR%%\r\n'
        channels = ['K', 'L', 'M', 'N', 'O', 'P', 'Q', 'R', 'S', 'T',
                    'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o', 'p', 'q', 'r', 's', 't']
        towrite.append(comm)
        for item in channels:
            comm = 'SR%s\r\n' % item
            towrite.append(comm)
        self.write_serial(''.join(towrite).encode("ascii"))

    def set_memory_banks_list(self, text):
        """"""
        match = re.fullmatch(r'MW ([A-Ta-t]):([0-9]+) TB\1(.*)', text)
        if match is None:
            raise ValueError('Invalid bank-list response')
        bank, channels, name = match.groups()
        item = ('%s:%s %s' % (bank, channels, name)).rstrip()
        items = self.cbx_lists.GetItems()
        if item in items:
            return
        # TB can start on either page; keep A, a, B, b, ... ordering.
        bank_order = (bank.upper(), bank.islower())
        position = len(items)
        for index, existing in enumerate(items):
            if re.match(r'[A-Ta-t]:', existing):
                existing_order = (existing[0].upper(), existing[0].islower())
                if existing_order > bank_order:
                    position = index
                    break
        items.insert(position, item)
        self.cbx_lists.SetItems(items)

    def set_memory_banks(self, item):
        """initializes and fills memory Bank ListControl
        """
        empty = re.fullmatch(r'MX([A-Ta-t][0-9]{2}) ---', item)
        if empty is not None:
            self.pending_memory_channels.discard(empty.group(1))
            self.filling_banks = bool(self.pending_memory_channels)
            return
        fields = item.split(None, 7)
        self.validate_fields(fields, ('MX', 'MP', 'RF', 'ST', 'AU', 'MD', 'AT', 'TM'))
        columns = fields
        columns = [item[2:] for item in columns]

        if columns[0] not in self.pending_memory_channels:
            return
        if columns[0] == self.last:
            return
        self.edit_list.list.InsertItem(self.edit_list.list.GetItemCount(), '')
        self.edit_list.list.fill_line(self.row, columns)

        self.last = columns[0]
        self.row += 1
        self.pending_memory_channels.remove(columns[0])
        self.filling_banks = bool(self.pending_memory_channels)

    def set_search_banks(self, item):
        """initializes and fills search Bank ListControl
        """
        columns = item.split(None, 7)
        if not re.fullmatch(r'SR[A-Ta-t]', columns[0]):
            raise ValueError('Invalid search bank')
        if len(columns) != 1:
            self.validate_fields(columns[1:6], ('SL', 'SU', 'ST', 'AU', 'MD'))
            if len(columns) == 8 and columns[6].startswith('AT'):
                self.validate_fields(columns[6:], ('AT', 'TT'))
            elif len(columns) not in (7, 8) or not columns[6].startswith('TT'):
                raise ValueError('Invalid search-bank name field')
        self.edit_list.list.InsertItem(self.edit_list.list.GetItemCount(), '')
        columns = [item[2:] for item in columns]

        if len(columns) < 4:
            self.edit_list.list.fill_line(self.row, columns)
            self.row += 1
            return
        l1 = list(columns[1])
        l1.insert(4, '.')
        l2 = list(columns[2])
        l2.insert(4, '.')
        columns[1] = ''.join(l1)
        columns[2] = ''.join(l2)

        self.edit_list.list.fill_line(self.row, columns)
        self.row += 1

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
            print('step with dot')
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
                print('text < %s >' % [textline])
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
