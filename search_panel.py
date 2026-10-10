__author__ = 'joaquin'

import sys
import wx
import wx.dataview as dv
import wx.lib.mixins.listctrl as listmix


class MemoryTableModel(dv.DataViewIndexListModel):
    """Native toggle cells backed only by confirmed scanner state."""
    def __init__(self, controller):
        super().__init__(0)
        self.controller = controller
        self.channels = []

    def GetColumnCount(self):
        return 9

    def GetColumnType(self, col):
        return 'bool' if col in (1, 2) else 'string'

    def GetValueByRow(self, row, col):
        value = self.controller.memory_row_values(self.channels[row])[col]
        return bool(value) if col in (1, 2) else value

    def HasValue(self, item, col):
        if not item.IsOk():
            return False
        if col not in (1, 2):
            return True
        channel = self.channels[self.GetRow(item)]
        return self.controller.memory_rows[channel]['select' if col == 1 else 'skip'] is not None

    def IsEnabledByRow(self, row, col):
        return col not in (1, 2) or self.controller.memory_flags_editable(self.channels[row])

    def GetAttrByRow(self, row, col, attr):
        if self.channels[row] != self.controller.receiving_channel:
            return False
        colour = wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHT)
        background = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
        attr.SetBackgroundColour(wx.Colour(*[(colour[i] + 4 * background[i]) // 5 for i in range(3)]))
        if col == 8:
            attr.SetBold(True)
        return True

    def SetValueByRow(self, value, row, col):
        if col in (1, 2) and self.IsEnabledByRow(row, col):
            self.controller.toggle_memory_flag(self.channels[row], 'select' if col == 1 else 'skip')
        # The native renderer must wait for scanner read-back, not commit locally.
        return False

    def clear(self):
        self.channels.clear()
        self.Reset(0)

    def append(self, channel):
        self.channels.append(channel)
        self.RowAppended()

    def insert(self, channel):
        row = sum(existing < channel for existing in self.channels)
        self.channels.insert(row, channel)
        for index, identifier in enumerate(self.channels):
            self.controller.memory_rows[identifier]['row'] = index
        self.RowInserted(row)

    def remove(self, channel):
        row = self.channels.index(channel)
        self.channels.pop(row)
        del self.controller.memory_rows[channel]
        for index, identifier in enumerate(self.channels):
            self.controller.memory_rows[identifier]['row'] = index
        self.RowDeleted(row)


class EditListCtrl(wx.ListCtrl,
                   listmix.ListCtrlAutoWidthMixin):

    def __init__(self, parent, id_, pos=wx.DefaultPosition,
                 size=wx.DefaultSize, style=0):
        wx.ListCtrl.__init__(self, parent, id_, pos, size, style)

        listmix.ListCtrlAutoWidthMixin.__init__(self)

        # for normal, simple columns, you can add them like this:
        for column in range(8):
            self.InsertColumn(column, "Column %i" % column)

        for row in range(5):
            newrow = self.InsertItem(self.GetItemCount(), '.')
            for column in range(8):
                self.SetItem(newrow, column, '.')

        # self.SetColumnWidth(0, wx.LIST_AUTOSIZE)

    def fill_line(self, row, data):
        for column, item in enumerate(data):
            self.SetItem(row, column, item)


class EditListCtrlPanel(wx.Panel):
    def __init__(self, parent):
        wx.Panel.__init__(self, parent, -1, style=wx.WANTS_CHARS)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.list = EditListCtrl(self, wx.ID_ANY,
                                 style=wx.LC_REPORT
                                 | wx.BORDER_NONE
                                 | wx.LC_VRULES
                                 | wx.LC_HRULES
                                 )

        sizer.Add(self.list, 1, wx.EXPAND)
        self.SetSizer(sizer)
        self.SetAutoLayout(True)

    def create_memory_view(self, controller):
        self.memory_model = MemoryTableModel(controller)
        self.memory = dv.DataViewCtrl(self, style=dv.DV_ROW_LINES | dv.DV_VERT_RULES | dv.DV_SINGLE)
        self.memory.AssociateModel(self.memory_model)
        for index, (label, width) in enumerate((('Channel', 65), ('Selected', 70), ('Skip', 50),
                                               ('Frequency', 130), ('Step', 90), ('Auto', 45),
                                               ('Mode', 55), ('Att', 40), ('Name', 150))):
            if index in (1, 2):
                self.memory.AppendToggleColumn(label, index, mode=dv.DATAVIEW_CELL_ACTIVATABLE, width=width)
            else:
                self.memory.AppendTextColumn(label, index, width=width)
        self.memory.Bind(wx.EVT_MOTION, self.on_memory_hover)
        self.memory.Bind(wx.EVT_LEAVE_WINDOW, self.on_memory_leave)
        self.memory.Hide()
        self.GetSizer().Add(self.memory, 1, wx.EXPAND)

    def on_memory_hover(self, event):
        item, column = self.memory.HitTest(event.GetPosition())
        tip = {1: 'Include this channel in Selected Channels.',
               2: 'Skip this channel during Stored Channels scanning.'}.get(
                   column.GetModelColumn() if item.IsOk() and column is not None else -1, '')
        current = self.memory.GetToolTipText()
        if tip != current:
            self.memory.SetToolTip(tip) if tip else self.memory.UnsetToolTip()
        event.Skip()

    def on_memory_leave(self, event):
        self.memory.UnsetToolTip()
        event.Skip()

    def show_memory(self, show):
        if hasattr(self, 'database'):
            self.database.Hide()
            self.database.set_active(False)
            self.SetMinSize((-1, 180))
        self.list.Show(not show)
        self.memory.Show(show)
        self.Layout()

    def create_database_view(self, controller):
        from frequency_database import FrequencyDatabasePanel
        self.database = FrequencyDatabasePanel(self, controller)
        self.database.Hide()
        self.GetSizer().Add(self.database, 1, wx.EXPAND)

    def show_database(self):
        self.list.Hide()
        self.memory.Hide()
        self.database.Show()
        self.database.set_active(True)
        self.SetMinSize((-1, 320))
        frame = self.GetParent()
        minimum = frame.GetSizer().GetMinSize()
        client = frame.GetClientSize()
        if client.height < minimum.height:
            frame.SetClientSize((max(client.width, minimum.width), minimum.height))
        frame.Layout()
        self.Layout()


class DummyFrame(wx.Frame):
    def __init__(self, *args, **kwds):
        # begin wxGlade: MyFrame.__init__
        kwds["style"] = wx.DEFAULT_FRAME_STYLE
        wx.Frame.__init__(self, *args, **kwds)
        self.button_1 = wx.Button(self, -1, "connect")
        self.tune = EditListCtrlPanel(self)

        self.__set_properties()
        self.__do_layout()

        self.Bind(wx.EVT_BUTTON, self.on_connect, self.button_1)

    def __set_properties(self):
        # begin wxGlade: MyFrame.__set_properties
        self.SetMinSize((700, 200))
        self.SetTitle("Test Frame")
        # end wxGlade

    def __do_layout(self):
        # begin wxGlade: MyFrame.__do_layout
        sizer_1 = wx.BoxSizer(wx.VERTICAL)
        sizer_2 = wx.BoxSizer(wx.HORIZONTAL)
        sizer_2.Add(self.button_1, 0, 0, 0)
        sizer_2.Add(self.tune, 1, wx.EXPAND, 0)
        sizer_1.Add(sizer_2, 1, wx.EXPAND, 0)
        self.SetSizer(sizer_1)
        sizer_1.Fit(self)
        self.Layout()

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def on_connect(self, evt):
        print('connect')


if __name__ == '__main__':
    print('hello')
    app = wx.PySimpleApp(0)
    wx.InitAllImageHandlers()
    frame_1 = DummyFrame(None, -1, "")
    # noinspection PyUnresolvedReferences
    app.SetTopWindow(frame_1)
    frame_1.Show()
    # noinspection PyUnresolvedReferences
    app.MainLoop()
