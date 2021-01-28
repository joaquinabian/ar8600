#!/usr/bin/env python
# -*- coding: CP1252 -*-
#
# generated by wxGlade 0.7.1RC1 on Sun Sep 13 19:53:54 2015
#

import wx

# begin wxGlade: dependencies
# end wxGlade
from tune_panel_class import TuningPanel
from search_panel import EditListCtrlPanel
# begin wxGlade: extracode
# end wxGlade


class AorCtrlFrame(wx.Frame):
    def __init__(self, *args, **kwds):
        # begin wxGlade: AorCtrlFrame.__init__
        wx.Frame.__init__(self, *args, **kwds)
        
        # Menu Bar
        self.aor_menubar = wx.MenuBar()
        self.mfile = wx.Menu()
        self.aor_menubar.Append(self.mfile, "File")
        self.medit = wx.Menu()
        self.aor_menubar.Append(self.medit, "Edit")
        self.SetMenuBar(self.aor_menubar)
        # Menu Bar end
        self.aor_status = self.CreateStatusBar(1)
        
        # Tool Bar
        self.aor_toolbar = wx.ToolBar(self, -1)
        self.SetToolBar(self.aor_toolbar)
        self.aor_toolbar.AddLabelTool(wx.ID_ANY, "newlog", wx.Bitmap("C:\\Python27\\programas\\aor\\icons\\new3.bmp", wx.BITMAP_TYPE_ANY), wx.NullBitmap, wx.ITEM_NORMAL, "", "")
        self.aor_toolbar.AddLabelTool(wx.ID_ANY, "open", wx.Bitmap("C:\\Python27\\programas\\aor\\icons\\load3.bmp", wx.BITMAP_TYPE_ANY), wx.NullBitmap, wx.ITEM_NORMAL, "", "")
        self.aor_toolbar.AddLabelTool(wx.ID_ANY, "upload", wx.Bitmap("C:\\Python27\\programas\\aor\\icons\\arrow-up_32.bmp", wx.BITMAP_TYPE_ANY), wx.NullBitmap, wx.ITEM_NORMAL, "", "")
        self.aor_toolbar.AddLabelTool(wx.ID_ANY, "connect", wx.Bitmap("C:\\Python27\\programas\\aor\\icons\\redo_32.bmp", wx.BITMAP_TYPE_ANY), wx.NullBitmap, wx.ITEM_NORMAL, "", "")
        self.aor_toolbar.AddLabelTool(wx.ID_ANY, "config", wx.Bitmap("C:\\Python27\\programas\\aor\\icons\\applications_32.bmp", wx.BITMAP_TYPE_ANY), wx.NullBitmap, wx.ITEM_NORMAL, "", "")
        # Tool Bar end
        self.panel_1 = wx.Panel(self, wx.ID_ANY)
        self.rb_vfos = wx.RadioBox(self.panel_1, wx.ID_ANY, "", choices=["VFO-A", "VFO-B", "VFO"], majorDimension=1, style=wx.RA_SPECIFY_COLS)
        self.panel_3 = wx.Panel(self.panel_1, wx.ID_ANY)
        self.lb_vfa = wx.StaticText(self.panel_3, wx.ID_ANY, "xxxxxxx........", style=wx.ALIGN_RIGHT|wx.ST_NO_AUTORESIZE)
        self.lb_vfb = wx.StaticText(self.panel_3, wx.ID_ANY, "xxxxxxx........", style=wx.ALIGN_RIGHT|wx.ST_NO_AUTORESIZE)
        self.lb_vfo = wx.StaticText(self.panel_3, wx.ID_ANY, "xxxxxxxx........", style=wx.ALIGN_RIGHT|wx.ST_NO_AUTORESIZE)
        self.sizer_12_staticbox = wx.StaticBox(self.panel_3, wx.ID_ANY, "")
        self.sizer_11_staticbox = wx.StaticBox(self.panel_1, wx.ID_ANY, "VFO Frequency [MHz]")
        self.ckbx_auto = wx.CheckBox(self.panel_1, wx.ID_ANY, "A")
        self.sizer_24_staticbox = wx.StaticBox(self.panel_1, wx.ID_ANY, "Auto")
        self.cbx_step = wx.ComboBox(self.panel_1, wx.ID_ANY, choices=["0.05", "0.1", "0.2", "0.5", "1", "5", "10", "50", "100"], style=wx.CB_DROPDOWN)
        self.sizer_25_staticbox = wx.StaticBox(self.panel_1, wx.ID_ANY, "Step")
        self.cbx_mode = wx.ComboBox(self.panel_1, wx.ID_ANY, choices=["WFM", "NFM", "SFM", "WAM", "AM", "NAM", "USB", "LSB", "CW"], style=wx.CB_DROPDOWN)
        self.sizer_252_staticbox = wx.StaticBox(self.panel_1, wx.ID_ANY, "Mode")
        self.cbx_lists = wx.ComboBox(self.panel_1, wx.ID_ANY, choices=["SEARCH BANKS", "SELECT SCAN", "PASS FREQS", "LOG VIEW", "DATABASE"], style=wx.CB_DROPDOWN)
        self.bt_refresh = wx.Button(self.panel_1, wx.ID_ANY, "Refresh")
        self.sizer_22_staticbox = wx.StaticBox(self.panel_1, wx.ID_ANY, "Lists")
        self.ckbx_att = wx.CheckBox(self.panel_1, wx.ID_ANY, "ATT")
        self.ckbx_nl = wx.CheckBox(self.panel_1, wx.ID_ANY, "NL")
        self.ckbx_afc = wx.CheckBox(self.panel_1, wx.ID_ANY, "AFC")
        self.sizer_17_staticbox = wx.StaticBox(self.panel_1, wx.ID_ANY, "Options")
        self.tuning_panel = TuningPanel(self.panel_1, wx.ID_ANY)
        self.sizer_5_staticbox = wx.StaticBox(self.panel_1, wx.ID_ANY, "Tuning")
        self.cbx_scan = wx.ComboBox(self.panel_1, wx.ID_ANY, choices=["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"], style=wx.CB_DROPDOWN)
        self.bt_scgrp = wx.Button(self.panel_1, wx.ID_ANY, "Scangroup")
        self.ckbx_sel = wx.CheckBox(self.panel_1, wx.ID_ANY, "SEL")
        self.ckbx_pas = wx.CheckBox(self.panel_1, wx.ID_ANY, "PAS")
        self.bt_start = wx.Button(self.panel_1, wx.ID_ANY, "Start")
        self.bt_stop = wx.Button(self.panel_1, wx.ID_ANY, "Stop/Mem")
        self.sizer_7_staticbox = wx.StaticBox(self.panel_1, wx.ID_ANY, "Scan")
        self.cbx_search = wx.ComboBox(self.panel_1, wx.ID_ANY, choices=["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "V"], style=wx.CB_DROPDOWN)
        self.bt_search = wx.Button(self.panel_1, wx.ID_ANY, "Searchgroup")
        self.bt_vfostart = wx.Button(self.panel_1, wx.ID_ANY, "VFO Start")
        self.bt_vfostop = wx.Button(self.panel_1, wx.ID_ANY, "VFO Stop")
        self.bt_mkpassfreq = wx.Button(self.panel_1, wx.ID_ANY, "Make Pass Frequency")
        self.sizer_8_staticbox = wx.StaticBox(self.panel_1, wx.ID_ANY, "Search")
        self.sql = wx.Slider(self.panel_1, wx.ID_ANY, 0, 0, 10, style=wx.SL_AUTOTICKS | wx.SL_HORIZONTAL | wx.SL_TOP)
        self.sizer_18_staticbox = wx.StaticBox(self.panel_1, wx.ID_ANY, "SQL")
        self.panel_2 = wx.Panel(self.panel_1, wx.ID_ANY)
        self.edit_list = EditListCtrlPanel(self)

        self.__set_properties()
        self.__do_layout()

        self.Bind(wx.EVT_TOOL, self.log_new, id=wx.ID_ANY)
        self.Bind(wx.EVT_TOOL, self.log_open, id=wx.ID_ANY)
        self.Bind(wx.EVT_TOOL, self.config, id=wx.ID_ANY)
        # end wxGlade

    def __set_properties(self):
        # begin wxGlade: AorCtrlFrame.__set_properties
        self.SetTitle("frame_1")
        self.aor_status.SetStatusWidths([-1])

        # statusbar fields
        aor_status_fields = ["frame_1_statusbar"]
        for i in range(len(aor_status_fields)):
            self.aor_status.SetStatusText(aor_status_fields[i], i)
        self.aor_toolbar.SetToolBitmapSize((5, 5))
        self.aor_toolbar.Realize()
        self.rb_vfos.SetMinSize((78, 100))
        self.rb_vfos.SetSelection(0)
        self.lb_vfa.SetFont(wx.Font(9, wx.DEFAULT, wx.NORMAL, wx.BOLD, 0, ""))
        self.lb_vfb.SetFont(wx.Font(9, wx.DEFAULT, wx.NORMAL, wx.BOLD, 0, ""))
        self.lb_vfo.SetFont(wx.Font(9, wx.DEFAULT, wx.NORMAL, wx.BOLD, 0, ""))
        self.panel_3.SetMinSize((195, 100))
        self.cbx_step.SetMinSize((75, 24))
        self.cbx_step.SetSelection(-1)
        self.cbx_mode.SetMinSize((75, 24))
        self.cbx_mode.SetSelection(1)
        self.cbx_lists.SetMinSize((130, 24))
        self.cbx_lists.SetSelection(-1)
        self.bt_refresh.SetMinSize((75, 26))
        self.tuning_panel.SetMinSize((166, 190))
        self.cbx_scan.SetMinSize((65, 24))
        self.cbx_scan.SetSelection(0)
        self.bt_scgrp.SetMinSize((85, 25))
        self.bt_start.SetMinSize((75, 25))
        self.bt_stop.SetMinSize((75, 25))
        self.cbx_search.SetMinSize((65, 24))
        self.cbx_search.SetSelection(0)
        self.bt_search.SetMinSize((85, 25))
        self.bt_vfostart.SetMinSize((75, 25))
        self.bt_vfostop.SetMinSize((75, 25))
        self.bt_mkpassfreq.SetMinSize((155, 25))
        self.sql.SetMinSize((209, 23))
        # end wxGlade

    def __do_layout(self):
        # begin wxGlade: AorCtrlFrame.__do_layout
        sizer_1 = wx.BoxSizer(wx.VERTICAL)
        sizer_2 = wx.BoxSizer(wx.HORIZONTAL)
        sizer_3 = wx.BoxSizer(wx.VERTICAL)
        self.sizer_18_staticbox.Lower()
        sizer_18 = wx.StaticBoxSizer(self.sizer_18_staticbox, wx.HORIZONTAL)
        sizer_4 = wx.BoxSizer(wx.HORIZONTAL)
        sizer_6 = wx.BoxSizer(wx.VERTICAL)
        self.sizer_8_staticbox.Lower()
        sizer_8 = wx.StaticBoxSizer(self.sizer_8_staticbox, wx.VERTICAL)
        sizer_19 = wx.BoxSizer(wx.HORIZONTAL)
        sizer_20 = wx.BoxSizer(wx.HORIZONTAL)
        self.sizer_7_staticbox.Lower()
        sizer_7 = wx.StaticBoxSizer(self.sizer_7_staticbox, wx.VERTICAL)
        sizer_26 = wx.BoxSizer(wx.HORIZONTAL)
        sizer_9 = wx.BoxSizer(wx.HORIZONTAL)
        sizer_27 = wx.BoxSizer(wx.HORIZONTAL)
        self.sizer_5_staticbox.Lower()
        sizer_5 = wx.StaticBoxSizer(self.sizer_5_staticbox, wx.VERTICAL)
        sizer_10 = wx.BoxSizer(wx.VERTICAL)
        sizer_16 = wx.BoxSizer(wx.HORIZONTAL)
        self.sizer_17_staticbox.Lower()
        sizer_17 = wx.StaticBoxSizer(self.sizer_17_staticbox, wx.VERTICAL)
        sizer_21 = wx.BoxSizer(wx.VERTICAL)
        self.sizer_22_staticbox.Lower()
        sizer_22 = wx.StaticBoxSizer(self.sizer_22_staticbox, wx.HORIZONTAL)
        sizer_23 = wx.BoxSizer(wx.HORIZONTAL)
        self.sizer_252_staticbox.Lower()
        sizer_252 = wx.StaticBoxSizer(self.sizer_252_staticbox, wx.HORIZONTAL)
        self.sizer_25_staticbox.Lower()
        sizer_25 = wx.StaticBoxSizer(self.sizer_25_staticbox, wx.HORIZONTAL)
        self.sizer_24_staticbox.Lower()
        sizer_24 = wx.StaticBoxSizer(self.sizer_24_staticbox, wx.HORIZONTAL)
        self.sizer_11_staticbox.Lower()
        sizer_11 = wx.StaticBoxSizer(self.sizer_11_staticbox, wx.HORIZONTAL)
        self.sizer_12_staticbox.Lower()
        sizer_12 = wx.StaticBoxSizer(self.sizer_12_staticbox, wx.VERTICAL)
        sizer_15 = wx.BoxSizer(wx.HORIZONTAL)
        sizer_14 = wx.BoxSizer(wx.HORIZONTAL)
        sizer_13 = wx.BoxSizer(wx.HORIZONTAL)
        sizer_11.Add(self.rb_vfos, 0, wx.BOTTOM | wx.EXPAND | wx.LEFT, 5)
        sizer_13.Add(self.lb_vfa, 1, wx.EXPAND, 0)
        sizer_12.Add(sizer_13, 1, wx.EXPAND | wx.RIGHT | wx.TOP, 5)
        sizer_14.Add(self.lb_vfb, 1, wx.EXPAND, 0)
        sizer_12.Add(sizer_14, 1, wx.EXPAND | wx.RIGHT, 5)
        sizer_15.Add(self.lb_vfo, 1, wx.EXPAND, 0)
        sizer_12.Add(sizer_15, 1, wx.EXPAND | wx.RIGHT, 5)
        self.panel_3.SetSizer(sizer_12)
        sizer_11.Add(self.panel_3, 1, 0, 0)
        sizer_10.Add(sizer_11, 0, wx.ALL, 5)
        sizer_24.Add(self.ckbx_auto, 0, wx.ALIGN_CENTER_VERTICAL | wx.BOTTOM | wx.TOP, 4)
        sizer_23.Add(sizer_24, 0, wx.EXPAND, 0)
        sizer_25.Add(self.cbx_step, 0, 0, 0)
        sizer_23.Add(sizer_25, 1, wx.LEFT, 5)
        sizer_252.Add(self.cbx_mode, 0, 0, 0)
        sizer_23.Add(sizer_252, 1, wx.LEFT, 5)
        sizer_21.Add(sizer_23, 0, wx.BOTTOM, 10)
        sizer_22.Add(self.cbx_lists, 0, wx.BOTTOM | wx.TOP, 1)
        sizer_22.Add(self.bt_refresh, 0, wx.BOTTOM | wx.LEFT, 5)
        sizer_21.Add(sizer_22, 0, wx.BOTTOM, 5)
        sizer_16.Add(sizer_21, 0, wx.ALL, 5)
        sizer_17.Add(self.ckbx_att, 0, wx.BOTTOM | wx.TOP, 5)
        sizer_17.Add(self.ckbx_nl, 0, wx.BOTTOM | wx.TOP, 5)
        sizer_17.Add(self.ckbx_afc, 0, wx.TOP, 5)
        sizer_16.Add(sizer_17, 0, wx.ALL | wx.EXPAND, 5)
        sizer_10.Add(sizer_16, 0, 0, 0)
        sizer_2.Add(sizer_10, 0, 0, 0)
        sizer_5.Add(self.tuning_panel, 0, wx.ADJUST_MINSIZE, 0)
        sizer_4.Add(sizer_5, 0, wx.ALL, 3)
        sizer_27.Add(self.cbx_scan, 0, wx.TOP, 1)
        sizer_27.Add(self.bt_scgrp, 0, wx.LEFT, 5)
        sizer_7.Add(sizer_27, 0, wx.BOTTOM, 5)
        sizer_9.Add(self.ckbx_sel, 0, 0, 0)
        sizer_9.Add(self.ckbx_pas, 0, wx.LEFT, 30)
        sizer_7.Add(sizer_9, 0, wx.BOTTOM, 5)
        sizer_26.Add(self.bt_start, 0, 0, 0)
        sizer_26.Add(self.bt_stop, 0, wx.LEFT, 5)
        sizer_7.Add(sizer_26, 0, 0, 5)
        sizer_6.Add(sizer_7, 0, wx.LEFT | wx.RIGHT | wx.TOP, 3)
        sizer_20.Add(self.cbx_search, 0, wx.TOP, 2)
        sizer_20.Add(self.bt_search, 0, wx.LEFT, 5)
        sizer_8.Add(sizer_20, 0, wx.BOTTOM, 5)
        sizer_19.Add(self.bt_vfostart, 0, 0, 0)
        sizer_19.Add(self.bt_vfostop, 0, wx.LEFT, 5)
        sizer_8.Add(sizer_19, 0, wx.BOTTOM, 5)
        sizer_8.Add(self.bt_mkpassfreq, 0, wx.BOTTOM, 2)
        sizer_6.Add(sizer_8, 0, wx.LEFT | wx.RIGHT, 3)
        sizer_4.Add(sizer_6, 0, 0, 0)
        sizer_3.Add(sizer_4, 0, wx.EXPAND | wx.LEFT, 0)
        sizer_18.Add(self.sql, 1, wx.BOTTOM | wx.EXPAND, 5)
        sizer_3.Add(sizer_18, 0, wx.BOTTOM | wx.EXPAND, 5)
        sizer_2.Add(sizer_3, 0, wx.LEFT, 5)
        sizer_2.Add(self.panel_2, 1, 0, 0)
        self.panel_1.SetSizer(sizer_2)
        sizer_1.Add(self.panel_1, 1, wx.EXPAND, 0)
        sizer_1.Add(self.edit_list, 1, wx.EXPAND, 0)
        self.SetSizer(sizer_1)
        sizer_1.Fit(self)
        self.Layout()
        # end wxGlade

    def log_new(self, event):  # wxGlade: AorCtrlFrame.<event_handler>
        print "Event handler 'log_new' not implemented!"
        event.Skip()

    def log_open(self, event):  # wxGlade: AorCtrlFrame.<event_handler>
        print "Event handler 'log_open' not implemented!"
        event.Skip()

    def config(self, event):  # wxGlade: AorCtrlFrame.<event_handler>
        print "Event handler 'config' not implemented!"
        event.Skip()

# end of class AorCtrlFrame
class MyApp(wx.App):
    def OnInit(self):
        aor_ctrl_frame = AorCtrlFrame(None, wx.ID_ANY, "")
        self.SetTopWindow(aor_ctrl_frame)
        aor_ctrl_frame.Show()
        return True

# end of class MyApp

if __name__ == "__main__":
    app = MyApp(0)
    app.MainLoop()