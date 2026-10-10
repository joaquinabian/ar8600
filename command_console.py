"""Small mirrored serial diagnostic window; all writes use the controller."""
from collections import deque

import wx


def monitoring_line(value):
    return value.startswith(('LM', 'LC'))


def command_bytes(command):
    if not isinstance(command, str) or not command.strip():
        raise ValueError('Enter one AR8600 command without CR/LF.')
    if '\r' in command or '\n' in command:
        raise ValueError('Embedded CR/LF is not allowed; send one command at a time.')
    try:
        return command.encode('ascii') + b'\r\n'
    except UnicodeEncodeError:
        raise ValueError('AR8600 commands must contain ASCII characters only.') from None


def send_command(controller, command):
    data = command_bytes(command)
    if not (controller.connected and controller.serial.is_open and controller.alive.is_set()):
        raise ValueError('Connect to the AR8600 before sending a command.')
    # Deliberately do not access serial.write directly: import/export and
    # temporary scan-group protection remain authoritative.
    if controller.write_serial(data) != len(data):
        raise ValueError('Command was not fully sent; see application status/serial diagnostics.')


class ConsoleHistory:
    def __init__(self, limit=500):
        self.lines = deque(maxlen=limit)

    def append(self, prefix, value):
        # Keep one history entry per serial line, including blank acknowledgements.
        visible = ''.join(char if 32 <= ord(char) < 127 else '\\x%02x' % ord(char) for char in value)
        self.lines.append(prefix + ' ' + visible)

    def text(self):
        return '\n'.join(self.lines)


class CommandConsole(wx.Frame):
    def __init__(self, controller):
        super().__init__(controller, title='AR8600 Command Console', size=(650, 380))
        self.controller = controller
        self.history = ConsoleHistory()
        self.manual_send = False
        panel = wx.Panel(self)
        self.command = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.command.SetToolTip('One ASCII AR8600 command without CR/LF; sent through the normal serial path.')
        self.send = wx.Button(panel, label='Send')
        self.release = wx.Button(panel, label='Normal Squelch / Release Audio')
        self.release.SetToolTip('Send MC0 once to return to normal squelch operation.')
        self.read_squelch = wx.Button(panel, label='Read Squelch State')
        self.read_squelch.SetToolTip('Read VFO voice/audio (DA) and signal-level (DB) thresholds without changing them. MC has no documented query.')
        self.show_monitoring = wx.CheckBox(panel, label='Show monitoring traffic (LM/LC)')
        self.show_monitoring.SetToolTip('Enable to see LM/LC replies, including manual queries: replies cannot reliably be distinguished from automatic monitoring.')
        clear = wx.Button(panel, label='Clear')
        self.output = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(self.command, 1, wx.EXPAND | wx.RIGHT, 5)
        row.Add(self.send, 0)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.Add(self.release, 0)
        buttons.Add(self.read_squelch, 0, wx.LEFT, 5)
        buttons.AddStretchSpacer()
        buttons.Add(clear, 0)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self.show_monitoring, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sizer.Add(self.output, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
        panel.SetSizer(sizer)
        self.send.Bind(wx.EVT_BUTTON, self.on_send)
        self.command.Bind(wx.EVT_TEXT_ENTER, self.on_send)
        self.release.Bind(wx.EVT_BUTTON, lambda event: self.send_text('MC0'))
        self.read_squelch.Bind(wx.EVT_BUTTON, self.on_read_squelch)
        clear.Bind(wx.EVT_BUTTON, self.on_clear)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.update_connection()

    def update_connection(self):
        enabled = (self.controller.connected and self.controller.serial.is_open and
                   self.controller.alive.is_set())
        for control in (self.command, self.send, self.release, self.read_squelch):
            control.Enable(enabled)

    def append(self, prefix, value):
        if (prefix in ('>', '<') and monitoring_line(value) and
                not self.show_monitoring.GetValue() and
                not (prefix == '>' and self.manual_send)):
            return
        self.history.append(prefix, value)
        self.output.ChangeValue(self.history.text())
        self.output.ShowPosition(self.output.GetLastPosition())

    def send_text(self, command):
        self.manual_send = True
        try:
            send_command(self.controller, command)
            if monitoring_line(command) and not self.show_monitoring.GetValue():
                self.append('!', 'Enable Show monitoring traffic (LM/LC) to see replies to this command.')
            return True
        except ValueError as error:
            self.append('!', str(error))
            return False
        finally:
            self.manual_send = False

    def on_read_squelch(self, event):
        # MC is set-only in the documented command table; do not invent a query.
        for command in ('DA', 'DB'):
            if not self.send_text(command):
                break

    def on_send(self, event):
        self.send_text(self.command.GetValue())

    def on_clear(self, event):
        self.history.lines.clear()
        self.output.Clear()

    def on_close(self, event):
        self.controller.command_console = None
        self.Destroy()
