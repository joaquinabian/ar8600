__author__ = 'joaquin'

import wx
from tune_panel import MyPanel


class TuningPanel(MyPanel):
    def __init__(self, *args, **kwds):
        MyPanel.__init__(self, *args, **kwds)

        self.tune_keyboard.Bind(wx.EVT_BUTTON, self.on_but)     # events only from keyboard
        self.tune_arrows.Bind(wx.EVT_BUTTON, self.on_arrows)    # events only from forward-reverse
        self.Bind(wx.EVT_BUTTON, self.on_enter, self.tune_benter)

        self.freq = 0

    def on_but(self, evt):
        obj = evt.GetEventObject()
        name = obj.GetLabel()
        if name in "1234567890.":
            self.tune_freq.write(name)

    # noinspection PyMethodMayBeStatic
    def on_arrows(self, evt):
        obj = evt.GetEventObject()
        name = obj.GetLabel()
        print 'xx ', name
        evt.Skip(True)

    def on_enter(self, evt):
        freq = self.tune_freq.GetValue()
        try:
            freq = float(freq)
        except ValueError:
            pass
        else:
            if 0.1 < freq < 3000:
                self.freq = freq
            else:
                return

            evt.Skip()


class MyFrame(wx.Frame):
    def __init__(self, *args, **kwds):
        # begin wxGlade: MyFrame.__init__
        kwds["style"] = wx.DEFAULT_FRAME_STYLE
        wx.Frame.__init__(self, *args, **kwds)
        self.button_1 = wx.Button(self, -1, "connect")
        self.tune = TuningPanel(self)

        self.__set_properties()
        self.__do_layout()

        self.Bind(wx.EVT_BUTTON, self.on_connect, self.button_1)

        # end wxGlade

    def __set_properties(self):
        # begin wxGlade: MyFrame.__set_properties
        self.SetTitle("Test Frame")
        # end wxGlade

    def __do_layout(self):
        # begin wxGlade: MyFrame.__do_layout
        sizer_1 = wx.BoxSizer(wx.VERTICAL)
        sizer_2 = wx.BoxSizer(wx.HORIZONTAL)
        sizer_2.Add(self.button_1, 0, 0, 0)
        sizer_2.Add(self.tune, 0, 0, 0)
        sizer_1.Add(sizer_2, 0, wx.EXPAND, 0)
        self.SetSizer(sizer_1)
        sizer_1.Fit(self)
        self.Layout()

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def on_connect(self, evt):
        print 'connect'

        #
        #

if __name__ == '__main__':
    print 'hello'
    app = wx.PySimpleApp(0)
    wx.InitAllImageHandlers()
    frame_1 = MyFrame(None, -1, "")
    # noinspection PyUnresolvedReferences
    app.SetTopWindow(frame_1)
    frame_1.Show()
    # noinspection PyUnresolvedReferences
    app.MainLoop()
