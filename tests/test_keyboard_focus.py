"""Focused native GUI checks; no application startup or serial-port access."""
from pathlib import Path
import ctypes
import sys
import time
import unittest

import wx
import wx.dataview as dv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from keyboard_focus import MainKeyboardFocus
from aor_control_frame import AorCtrlFrame
import test_current_bank_scan as scan_tests


class KeyboardGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)
        cls.loop = wx.GUIEventLoop()
        cls.activator = wx.EventLoopActivator(cls.loop)
        cls.user32 = ctypes.windll.user32
        cls.user32.PostMessageW.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)

    def pump(self):
        for _ in range(8):
            while self.loop.Pending():
                self.loop.Dispatch()
            wx.Yield()
            time.sleep(0.01)

    def setUp(self):
        self.frame = wx.Frame(None, title='AR8600 keyboard focus check', pos=(40, 40), size=(920, 560))
        self.panel = wx.Panel(self.frame)
        self.slider = wx.Slider(self.panel, value=100, minValue=0, maxValue=255, pos=(10, 10), size=(280, 35))
        self.choice = wx.Choice(self.panel, choices=['A', 'B', 'C'], pos=(310, 10))
        self.choice.SetSelection(0)
        self.combo = wx.ComboBox(self.panel, choices=['One', 'Two', 'Three'], style=wx.CB_READONLY, pos=(430, 10))
        self.combo.SetSelection(0)
        self.text = wx.TextCtrl(self.panel, value='abc', pos=(610, 10))
        self.button = wx.Button(self.panel, label='Next', pos=(750, 10))
        self.table = wx.ListCtrl(self.panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL,
                                 pos=(10, 65), size=(250, 155))
        self.table.InsertColumn(0, 'Channel')
        self.data = dv.DataViewListCtrl(self.panel, pos=(280, 65), size=(250, 155))
        self.data.AppendTextColumn('Memory / Database')
        for index in range(40):
            self.table.InsertItem(index, 'A%02d' % index)
            self.data.AppendItem(['Row %d' % index])
        self.changed = []
        self.slider.Bind(wx.EVT_SLIDER, lambda event: self.changed.append(self.slider.GetValue()))
        self.keyboard = MainKeyboardFocus(self.frame, self.slider)
        self.frame.Show()
        self.frame.Raise()
        self.frame.SetFocus()
        self.pump()

    def tearDown(self):
        self.frame.Destroy()
        self.pump()

    def focus(self, control):
        control.SetFocus()
        self.pump()
        focus = wx.Window.FindFocus()
        self.assertTrue(focus == control or control.IsDescendant(focus))

    def key(self, code, modifiers=wx.MOD_NONE):
        # Drive wx's CHAR_HOOK followed by the native key message only if
        # allowed. This avoids foreground-desktop restrictions and sends no
        # keyboard input to the user's other applications.
        focus = wx.Window.FindFocus()
        hook = wx.KeyEvent(wx.EVT_CHAR_HOOK.typeId)
        hook.SetKeyCode(code)
        hook.SetShiftDown(bool(modifiers & wx.MOD_SHIFT))
        hook.SetEventObject(focus)
        self.frame.GetEventHandler().ProcessEvent(hook)
        if hook.GetSkipped() or hook.IsNextEventAllowed():
            virtual_key = {wx.WXK_LEFT: 0x25, wx.WXK_UP: 0x26, wx.WXK_RIGHT: 0x27,
                           wx.WXK_DOWN: 0x28, wx.WXK_TAB: 0x09, wx.WXK_RETURN: 0x0d}[code]
            state = (ctypes.c_ubyte * 256)()
            self.user32.GetKeyboardState(state)
            previous = bytes(state)
            state[0x10] = 0x80 if modifiers & wx.MOD_SHIFT else 0
            self.user32.SetKeyboardState(state)
            self.assertTrue(self.user32.PostMessageW(focus.GetHandle(), 0x100, virtual_key, 1))
            self.pump()
            self.assertTrue(self.user32.PostMessageW(focus.GetHandle(), 0x101, virtual_key, (3 << 30) | 1))
            self.pump()
            self.user32.SetKeyboardState((ctypes.c_ubyte * 256).from_buffer_copy(previous))
        self.pump()

    def test_slider_arrows_single_unit_and_endpoints_keep_focus(self):
        self.focus(self.slider)
        for key, expected in ((wx.WXK_LEFT, 99), (wx.WXK_RIGHT, 100),
                              (wx.WXK_DOWN, 99), (wx.WXK_UP, 100)):
            self.key(key)
            self.assertEqual(self.slider.GetValue(), expected)
            self.assertEqual(wx.Window.FindFocus(), self.slider)
        self.assertEqual(self.changed, [99, 100, 99, 100])
        for value, keys in ((0, (wx.WXK_LEFT, wx.WXK_DOWN)),
                            (255, (wx.WXK_RIGHT, wx.WXK_UP))):
            self.slider.SetValue(value)
            for key in keys:
                for _ in range(3):
                    self.key(key)
                    self.assertEqual(self.slider.GetValue(), value)
                    self.assertEqual(wx.Window.FindFocus(), self.slider)
        self.assertEqual(self.choice.GetSelection(), 0)
        self.assertEqual(self.combo.GetSelection(), 0)
        self.assertEqual(len(self.changed), 4)

    def test_pending_slider_readback_cannot_transfer_arrows_to_selector(self):
        self.focus(self.slider)
        self.keyboard.enable_slider(False)
        self.pump()
        self.assertEqual(wx.Window.FindFocus(), self.keyboard.neutral)
        self.key(wx.WXK_DOWN)
        self.assertEqual(self.choice.GetSelection(), 0)
        self.keyboard.enable_slider(True)
        self.pump()
        self.assertEqual(wx.Window.FindFocus(), self.slider)
        self.key(wx.WXK_RIGHT)
        self.assertEqual(self.slider.GetValue(), 101)
        self.keyboard.enable_slider(False)
        self.focus(self.combo)
        self.keyboard.enable_slider(True)
        self.assertEqual(wx.Window.FindFocus(), self.combo)

    def test_slider_tab_traversal_remains_normal(self):
        self.focus(self.slider)
        self.key(wx.WXK_TAB)
        self.assertEqual(wx.Window.FindFocus(), self.choice)
        self.key(wx.WXK_TAB, wx.MOD_SHIFT)
        self.assertEqual(wx.Window.FindFocus(), self.slider)

    def test_main_squelch_is_compact_and_in_left_column(self):
        frame = AorCtrlFrame(None)
        try:
            frame.Show()
            frame.Layout()
            self.pump()
            left = frame.panel_1.GetSizer().GetItem(0).GetSizer()
            box = frame.sql.GetContainingSizer()
            self.assertTrue(any(item.GetSizer() == box for item in left.GetChildren()))
            self.assertEqual(box.GetItem(frame.sql).GetProportion(), 0)
            self.assertGreaterEqual(frame.sql.GetSize().width, 200)
            self.assertLessEqual(frame.sql.GetSize().width, 250)
            self.assertGreater(frame.sizer_18_staticbox.GetPosition().y,
                               frame.sizer_17_staticbox.GetPosition().y)
            self.assertLess(frame.sql.GetPosition().x, frame.tuning_panel.GetPosition().x)
        finally:
            frame.Destroy()
            self.pump()

    def test_keyboard_squelch_remains_smooth_during_actual_handler_readback(self):
        ctrl, _, _ = scan_tests.GuiBoundaryTests().smooth_level_controller()
        ctrl.sql = self.slider
        ctrl.sql_value = wx.StaticText(self.panel, label='100', pos=(10, 240))
        ctrl.keyboard_focus = self.keyboard
        self.slider.Unbind(wx.EVT_SLIDER)
        self.slider.Bind(wx.EVT_SLIDER, ctrl.on_level_squelch_change)
        self.focus(self.slider)
        self.key(wx.WXK_RIGHT)
        ctrl.on_level_squelch_timer(None)
        self.assertEqual(self.slider.GetValue(), 101)
        self.assertEqual(wx.Window.FindFocus(), self.slider)
        self.key(wx.WXK_UP)
        self.key(wx.WXK_RIGHT)
        self.key(wx.WXK_LEFT)
        self.assertEqual(self.slider.GetValue(), 102)
        self.assertEqual(ctrl.sql_value.GetLabel(), '102')
        ctrl.on_level_squelch_timer(None)
        self.assertEqual(ctrl.commands, [b'DB101\r\nDB\r\n'])
        ctrl.accept_operation_parameter('DB101')
        self.assertEqual(ctrl.commands[-1], b'DB102\r\nDB\r\n')
        self.assertEqual(self.slider.GetValue(), 102)
        ctrl.accept_operation_parameter('DB102')
        self.assertEqual(self.slider.GetValue(), 102)
        self.assertEqual(wx.Window.FindFocus(), self.slider)

    def test_empty_click_neutralizes_focus_and_arrows(self):
        self.focus(self.choice)
        coordinates = (350 << 16) | 700
        self.user32.PostMessageW(self.panel.GetHandle(), 0x201, 1, coordinates)
        self.user32.PostMessageW(self.panel.GetHandle(), 0x202, 0, coordinates)
        self.pump()
        self.assertEqual(wx.Window.FindFocus(), self.keyboard.neutral)
        for key in (wx.WXK_UP, wx.WXK_DOWN, wx.WXK_LEFT, wx.WXK_RIGHT):
            self.key(key)
        self.assertEqual(self.choice.GetSelection(), 0)
        self.assertEqual(self.combo.GetSelection(), 0)
        self.assertEqual(self.slider.GetValue(), 100)
        self.key(wx.WXK_TAB)
        self.assertNotEqual(wx.Window.FindFocus(), self.keyboard.neutral)

    def test_choices_and_text_keep_local_behavior(self):
        self.focus(self.choice)
        self.key(wx.WXK_DOWN)
        self.key(wx.WXK_RETURN)
        self.assertEqual(self.choice.GetSelection(), 1)
        self.assertEqual(self.combo.GetSelection(), 0)
        self.focus(self.combo)
        self.key(wx.WXK_DOWN)
        self.assertEqual(self.combo.GetSelection(), 1)
        self.assertEqual(self.choice.GetSelection(), 1)
        self.focus(self.text)
        self.text.SetInsertionPointEnd()
        self.key(wx.WXK_LEFT)
        self.assertEqual(self.text.GetInsertionPoint(), 2)
        self.assertEqual(self.text.GetValue(), 'abc')
        self.key(wx.WXK_TAB)
        self.assertEqual(wx.Window.FindFocus(), self.button)
        self.key(wx.WXK_TAB, wx.MOD_SHIFT)
        self.assertEqual(wx.Window.FindFocus(), self.text)

    def test_focused_tables_select_and_scroll_locally(self):
        self.table.Select(0)
        self.table.Focus(0)
        self.focus(self.table)
        self.key(wx.WXK_DOWN)
        self.assertEqual(self.table.GetFirstSelected(), 1)
        for _ in range(16):
            self.key(wx.WXK_DOWN)
        self.assertEqual(self.table.GetFirstSelected(), 17)
        self.assertGreater(self.table.GetTopItem(), 0)
        self.key(wx.WXK_UP)
        self.assertEqual(self.table.GetFirstSelected(), 16)
        self.data.SelectRow(0)
        self.focus(self.data)
        self.key(wx.WXK_DOWN)
        self.assertEqual(self.data.GetSelectedRow(), 1)
        self.key(wx.WXK_UP)
        self.assertEqual(self.data.GetSelectedRow(), 0)
        self.assertEqual(self.choice.GetSelection(), 0)
        self.assertEqual(self.combo.GetSelection(), 0)


if __name__ == '__main__':
    unittest.main()
