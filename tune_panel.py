#!/usr/bin/env python
# -*- coding: CP1252 -*-
#
# generated by wxGlade 0.7.1RC1 on Wed Sep 09 17:42:34 2015
#

import wx

# begin wxGlade: dependencies
# end wxGlade

# begin wxGlade: extracode
# end wxGlade


class MyPanel(wx.Panel):
    def __init__(self, *args, **kwds):
        # begin wxGlade: MyPanel.__init__
        kwds["style"] = wx.TAB_TRAVERSAL
        wx.Panel.__init__(self, *args, **kwds)
        self.tune_arrows = wx.Panel(self, wx.ID_ANY)
        self.tune_frev = wx.Button(self.tune_arrows, wx.ID_ANY, "<<")
        self.tune_rev = wx.Button(self.tune_arrows, wx.ID_ANY, "<")
        self.tune_forw = wx.Button(self.tune_arrows, wx.ID_ANY, ">")
        self.tune_ffor = wx.Button(self.tune_arrows, wx.ID_ANY, ">>")
        self.tune_freq = wx.TextCtrl(self, wx.ID_ANY, "")
        self.tune_keyboard = wx.Panel(self, wx.ID_ANY)
        self.tune_b1 = wx.Button(self.tune_keyboard, wx.ID_ANY, "1")
        self.tune_b2 = wx.Button(self.tune_keyboard, wx.ID_ANY, "2")
        self.tune_b3 = wx.Button(self.tune_keyboard, wx.ID_ANY, "3")
        self.tune_b4 = wx.Button(self.tune_keyboard, wx.ID_ANY, "4")
        self.tune_b5 = wx.Button(self.tune_keyboard, wx.ID_ANY, "5")
        self.tune_b6 = wx.Button(self.tune_keyboard, wx.ID_ANY, "6")
        self.tune_b7 = wx.Button(self.tune_keyboard, wx.ID_ANY, "7")
        self.tune_b8 = wx.Button(self.tune_keyboard, wx.ID_ANY, "8")
        self.tune_b9 = wx.Button(self.tune_keyboard, wx.ID_ANY, "9")
        self.tune_bdot = wx.Button(self.tune_keyboard, wx.ID_ANY, ".")
        self.tune_b0 = wx.Button(self.tune_keyboard, wx.ID_ANY, "0")
        self.tune_back = wx.Button(self.tune_keyboard, wx.ID_ANY, "<-")
        self.tune_benter = wx.Button(self, wx.ID_ANY, "Enter")

        self.__set_properties()
        self.__do_layout()
        # end wxGlade

    def __set_properties(self):
        # begin wxGlade: MyPanel.__set_properties
        self.SetSize((166, 199))
        self.tune_frev.SetMinSize((37, 26))
        self.tune_rev.SetMinSize((37, 26))
        self.tune_forw.SetMinSize((37, 26))
        self.tune_ffor.SetMinSize((37, 26))
        self.tune_freq.SetMinSize((165, 24))
        self.tune_b1.SetMinSize((55, 27))
        self.tune_b2.SetMinSize((55, 27))
        self.tune_b3.SetMinSize((55, 27))
        self.tune_b4.SetMinSize((55, 27))
        self.tune_b5.SetMinSize((55, 27))
        self.tune_b6.SetMinSize((55, 27))
        self.tune_b7.SetMinSize((55, 27))
        self.tune_b8.SetMinSize((55, 27))
        self.tune_b9.SetMinSize((55, 27))
        self.tune_bdot.SetMinSize((55, 27))
        self.tune_b0.SetMinSize((55, 27))
        self.tune_back.SetMinSize((55, 27))
        self.tune_benter.SetMinSize((165, 28))
        # end wxGlade

    def __do_layout(self):
        # begin wxGlade: MyPanel.__do_layout
        sizer_1 = wx.BoxSizer(wx.VERTICAL)
        grid_sizer_1 = wx.GridSizer(4, 3, 0, 0)
        sizer_2 = wx.BoxSizer(wx.HORIZONTAL)
        sizer_2.Add(self.tune_frev, 0, wx.RIGHT, 4)
        sizer_2.Add(self.tune_rev, 0, wx.LEFT | wx.RIGHT, 1)
        sizer_2.Add(self.tune_forw, 0, wx.LEFT | wx.RIGHT, 1)
        sizer_2.Add(self.tune_ffor, 0, wx.LEFT, 4)
        self.tune_arrows.SetSizer(sizer_2)
        sizer_1.Add(self.tune_arrows, 0, 0, 0)
        sizer_1.Add(self.tune_freq, 0, 0, 0)
        grid_sizer_1.Add(self.tune_b1, 0, 0, 0)
        grid_sizer_1.Add(self.tune_b2, 0, 0, 0)
        grid_sizer_1.Add(self.tune_b3, 0, 0, 0)
        grid_sizer_1.Add(self.tune_b4, 0, 0, 0)
        grid_sizer_1.Add(self.tune_b5, 0, 0, 0)
        grid_sizer_1.Add(self.tune_b6, 0, 0, 0)
        grid_sizer_1.Add(self.tune_b7, 0, 0, 0)
        grid_sizer_1.Add(self.tune_b8, 0, 0, 0)
        grid_sizer_1.Add(self.tune_b9, 0, 0, 0)
        grid_sizer_1.Add(self.tune_bdot, 0, 0, 0)
        grid_sizer_1.Add(self.tune_b0, 0, 0, 0)
        grid_sizer_1.Add(self.tune_back, 0, 0, 0)
        self.tune_keyboard.SetSizer(grid_sizer_1)
        sizer_1.Add(self.tune_keyboard, 0, 0, 0)
        sizer_1.Add(self.tune_benter, 0, 0, 0)
        self.SetSizer(sizer_1)
        self.Layout()
        # end wxGlade

# end of class MyPanel
