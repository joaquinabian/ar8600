import wx
import sys
import serial_conf_dialog
import serial
import threading
import re
from aor_control_frame import AorCtrlFrame
from aor_functions import do_nothing, format_frequency


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
        AorCtrlFrame.__init__(self, *args, **kwds)

        self.__set_properties()
        self.__attach_events()           # register events

    def start_thread(self):
        """Start the receiver thread"""
        self.thread = threading.Thread(target=self.com_thread)
        self.thread.daemon = True
        self.alive.set()
        self.thread.start()

    def stop_thread(self):
        """Stop the receiver thread, wait util it's finished."""
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
            pass
        elif selection == 'PASS FREQS':
            pass
        elif selection == 'LOG VIEW':
            pass
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
                # print 'event text ', text
                if first.startswith(('VA ', 'VB ', 'VF ')):
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
                    self.set_search_banks(first)
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
        if received_valid:
            self.connected = True

    def write_serial(self, data):
        try:
            return self.serial.write(data)
        except serial.SerialException as error:
            print('Serial write error: %s' % error, file=sys.stderr)

    def close_serial(self):
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
            textline = textline.replace('\r\n', "").strip()
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
