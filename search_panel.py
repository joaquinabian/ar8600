__author__ = 'joaquin'

import sys
import wx
import wx.lib.mixins.listctrl as listmix


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
            newrow = self.InsertStringItem(sys.maxint, '.')
            for column in range(8):
                self.SetStringItem(newrow, column, '.')

        # self.SetColumnWidth(0, wx.LIST_AUTOSIZE)

    def fill_line(self, row, data):
        for column, item in enumerate(data):
            self.SetStringItem(row, column, item)


class EditListCtrlPanel(wx.Panel):
    def __init__(self, parent):
        wx.Panel.__init__(self, parent, -1, style=wx.WANTS_CHARS)
        new_id = wx.NewId()
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.list = EditListCtrl(self, new_id,
                                 style=wx.LC_REPORT
                                 | wx.BORDER_NONE
                                 | wx.LC_VRULES
                                 | wx.LC_HRULES
                                 )

        sizer.Add(self.list, 1, wx.EXPAND)
        self.SetSizer(sizer)
        self.SetAutoLayout(True)


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
        print 'connect'


if __name__ == '__main__':
    print 'hello'
    app = wx.PySimpleApp(0)
    wx.InitAllImageHandlers()
    frame_1 = DummyFrame(None, -1, "")
    # noinspection PyUnresolvedReferences
    app.SetTopWindow(frame_1)
    frame_1.Show()
    # noinspection PyUnresolvedReferences
    app.MainLoop()
