import wx
import sys
import serial_conf_dialog
import serial
import threading
from aor_control_frame import AorCtrlFrame
from aor_functions import do_nothing, format_frequency


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
        self.__class__(self.GetId(), self.data)


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
        self.memory_banks = []
        self.connected = False
        self.filling_banks = False
        self.list_item_clicked = None
        self.alive = threading.Event()
        AorCtrlFrame.__init__(self, *args, **kwds)

        self.__set_properties()
        self.__attach_events()           # register events

    def start_thread(self):
        """Start the receiver thread"""
        self.thread = threading.Thread(target=self.com_thread)
        self.thread.setDaemon(1)
        self.alive.set()
        self.thread.start()

    def stop_thread(self):
        """Stop the receiver thread, wait util it's finished."""
        if self.thread is not None:
            self.alive.clear()          # clear alive event for thread
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
            print 'some other tool pressed'

    def open_menu(self, event):
        """"""
        self.list_item_clicked = event.GetText()
        x, y = event.GetPoint()

        menu = wx.Menu()
        for (id_, title) in menu_title_by_id.items():
            menu.Append(id_, title)
            wx.EVT_MENU(menu, id_, self.menu_selection_cb)
        self.edit_list.list.PopupMenu(menu, (x+10, y))
        menu.Destroy()

    def menu_selection_cb(self, event):
        # do something
        operation = menu_title_by_id[event.GetId()]
        target = self.list_item_clicked
        print 'Perform "%s" on "%s."' % (operation, target)

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

        self.serial.write(func)
        self.serial.write('RX\r\n')

    def on_auto(self, evt):
        if self.ckbx_auto.IsChecked():
            self.serial.write('AU1\r\nRX\r\n')
        else:
            self.serial.write('AU0\r\n')

    def on_enter_att(self, evt):
        """Changes Attenuation ON/OFF
        """
        att = 1 if self.ckbx_att.IsChecked() else 0
        self.serial.write('AT%s\r\n' % att)

    def on_select_vfo(self, evt):
        """Select working vfo"""
        selection = self.rb_vfos.GetSelection()
        comm = []
        if selection == 0:
            if not self.vfb:
                comm.append('VB\r\nRX\r\n')
            comm.append('VA\r\n')
            if not self.vfa:
                comm.append('RX\r\n')
        elif selection == 1:
            if not self.vfa:
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
        self.serial.write(towrite)

    def on_vfo_start(self, evt):
        self.serial.write('VS\r\n')

    def on_vfo_stop(self, evt):
        self.serial.write('VV0\r\n')

    def on_select_mode(self, evt):
        """Set mode on RX
        """
        mode = self.cbx_mode.GetSelection()
        self.serial.write('MD%s\r\n' % mode)

    def on_select_step(self):
        """Set step on RX
        STnnnnm0<CR> Set the tuning step size in Hz
        STnnn.nm<CR> Set the tuning step size in kHz
        """
        step = float(self.cbx_step.GetStringSelection())
        self.serial.write('ST%06.2f\r\n' % step)

    def on_enter_freq(self, evt):
        """Writes command RF to serial
        """
        freq = self.tuning_panel.freq
        dot_pos = 4
        comm = 'UNK'
        # print freq
        # RFnnnnnnnnm0 (Hz)
        # RFnnnn.nnnnm (MHz)
        if freq > 3:
            alist = ['0', '0', '0', '0', '.', '0', '0', '0', '0', '0']

            freq = str(freq)
            start = dot_pos - freq.find('.')
            for char_ in freq:
                alist[start] = char_
                start += 1
            text = ''.join(alist)
            comm = 'RF%s\r\n' % text

        self.serial.write(comm)

    def on_select_list(self, evt):
        selection = self.cbx_lists.GetStringSelection()
        print selection
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
        for first in event.data:
            # print 'event text ', text
            if first.startswith('VF '):
                self.set_vfo_text(first, 2)
            elif first.startswith('VB '):
                self.set_vfo_text(first, 1)
                if not self.connected:
                    self.serial.write('VA\r\nRX\r\nVB\r\n')
                    self.rb_vfos.SetSelection(1)
            elif first.startswith('VA '):
                self.set_vfo_text(first, 0)
                if not self.connected:
                    self.serial.write('VB\r\nRX\r\nVA\r\n')
                    self.rb_vfos.SetSelection(0)
            elif first.startswith('SR'):
                self.set_search_banks(first)
            elif first.startswith('MW'):
                self.set_memory_banks_list(first)
            elif first.startswith('MR '):
                if self.connected and self.filling_banks:
                    self.set_memory_banks(first)
            else:
                print 'nothing'

        self.connected = True
        self.filling_banks = False

    def log_new(self, event):  # wxGlade: AorCtrlFrame.<event_handler>
        print "log_new"

    def log_open(self, event):  # wxGlade: AorCtrlFrame.<event_handler>
        print "log_open"

    # noinspection PyPep8Naming
    def OnExit(self, event):
        """Menu point Exit"""
        self.Close()

    def on_close(self, event):
        """Called on application shutdown."""
        self.stop_thread()               # stop reader thread
        self.serial.close()             # cleanup
        self.Destroy()                  # close windows, exit app

    def set_port(self):
        """Show the port settings dialog. The reader thread is stopped for the
           settings change.
        """
        self.stop_thread()
        self.serial.close()

        dialog_serial_cfg = serial_conf_dialog.SerialConfigDialog(None, -1, "", serial=self.serial)
        dialog_serial_cfg.ShowModal()
        dialog_serial_cfg.Destroy()

    def connect(self):
        """"""
        self.stop_thread()
        self.serial.close()

        try:
            self.serial.open()
        except serial.SerialException, e:
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
            self.serial.write('RX\r\n')
            self.serial.write('TB\r\n')
            self.serial.write('TB\r\n')

    def get_memory_banks(self):
        """
        MA[bank]             reads 10 channels of bank
        MR[bank][channel]    recall channel in bank (sets in rx but doesn`t return anything but ? for empty channel
        RX                   reads memory bank if in memory manual
                             MR MX[bank][channel] MP[pass] RF[rf] ST AU MD AT TM
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

        towrite = []
        for channel in range(int(channels)):
            comm = 'MR%s%02i\r\n' % (bank, channel)
            towrite.append(comm.encode())
            towrite.append('RX\r\n')
        # print 'towrite ', towrite
        self.serial.write(''.join(towrite))

    def get_search_banks(self):
        """
        SRx<CR> where x = A-T or a-t  -> Recalls search bank x
        SR%%<CR>                      -> Responds with a listing of all search banks A-J
        Responds with:
        SRx SLnnnnnnnnnn SUnnnnnnnnnn STnnnnnn AUn MDn TTxxx...x
        """
        self.row = 0
        self.edit_list.list.ClearAll()
        # set column names
        column_headers = ['Bank', 'Start', 'Stop', 'Step', 'AUTO', 'MODE', 'ATT', 'NAME']
        for column, label in enumerate(column_headers):
            self.edit_list.list.InsertColumn(column, label)

        towrite = []
        comm = 'SR%%\r\n'
        channels = ['K', 'L', 'M', 'N', 'O', 'P', 'Q', 'R', 'S', 'T',
                    'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'o', 'p', 'q', 'r', 's', 't']
        towrite.append(comm)
        for item in channels:
            comm = 'SR%s\r\n' % item
            towrite.append(comm)
        self.serial.write(''.join(towrite))

    def set_memory_banks_list(self, text):
        """"""
        item = text.replace('TB', '')[3:]
        a, b = item.split(None, 1)
        item = a + ' ' + b[1:]
        items = self.cbx_lists.GetItems()
        items.append(item)
        self.cbx_lists.SetItems(items)

    def set_memory_banks(self, item):
        """initializes and fills memory Bank ListControl
        """
        columns = item.split(None, 8)[1:]
        columns = [item[2:] for item in columns]

        if columns[0] == self.last:
            return
        self.edit_list.list.InsertStringItem(sys.maxint, '')
        self.edit_list.list.fill_line(self.row, columns)

        self.last = columns[0]
        self.row += 1

    def set_search_banks(self, item):
        """initializes and fills search Bank ListControl
        """
        self.edit_list.list.InsertStringItem(sys.maxint, '')
        columns = item.split(None, 7)
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

    def set_vfo_text(self, text, vfx):
        data = text.split()[1:]
        freq, step, auto, mode, att = (item[2:] for item in data)
        self.rb_vfos.SetSelection(vfx)
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
        # step
        if '.' in step:
            print 'step with dot'
            # in megaherz
        else:
            # in kilos
            step = float(step) / 1000
            values = self.cbx_step.GetItems()
            for index, value in enumerate(values):
                if float(value) == step:
                    self.cbx_step.SetSelection(index)
                    break
        # auto
        self.ckbx_auto.SetValue(int(auto))
        # mode
        self.cbx_mode.SetSelection(int(mode))
        # att
        self.ckbx_att.SetValue(int(att))

        self.lb_vfo.Hide()
        self.lb_vfo.Show()

    def com_thread(self):
        """Thread that handles the incoming traffic. Does the basic input
           transformation (newlines) and generates an SerialRxEvent"""
        while self.alive.isSet():
            # time.sleep(0.2)
            text_lines = self.serial.readlines()
            # print text_lines
            text_lines = [textline.replace('\r\n', "").strip() for textline in text_lines if textline != '?\r\n']

            if text_lines:
                print 'text < %s >' % text_lines
                event = SerialRxEvent(self.GetId(), text_lines)
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
