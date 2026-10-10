"""Main-window keyboard focus rules; no scanner commands or global stepping."""
import wx


ARROWS = {wx.WXK_LEFT: -1, wx.WXK_DOWN: -1,
          wx.WXK_RIGHT: 1, wx.WXK_UP: 1}


class NeutralFocus(wx.Control):
    """Invisible, mouse-selected focus sink, excluded from normal Tab traversal."""
    def AcceptsFocus(self):
        return True

    def AcceptsFocusFromKeyboard(self):
        return False


class MainKeyboardFocus:
    def __init__(self, frame, slider):
        self.frame = frame
        self.slider = slider
        self.restore_slider_focus = False
        self.neutral = NeutralFocus(slider.GetParent(), pos=(-1, -1), size=(0, 0), style=wx.WANTS_CHARS)
        self.neutral.Bind(wx.EVT_KEY_DOWN, self.on_neutral_key)
        slider.SetWindowStyleFlag(slider.GetWindowStyleFlag() | wx.WANTS_CHARS)
        slider.Bind(wx.EVT_KEY_DOWN, self.on_slider_key)
        frame.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)
        self.bind_backgrounds(frame)

    def bind_backgrounds(self, window):
        # Stop at interactive controls: their internal child windows are not
        # empty application panels (e.g. DataView's scrolling window).
        if isinstance(window, (wx.Panel, wx.Frame, wx.StaticBox, wx.StaticText)):
            window.Bind(wx.EVT_LEFT_DOWN, self.on_background_click)
            if isinstance(window, (wx.Panel, wx.Frame)):
                window.Bind(wx.EVT_NAVIGATION_KEY, self.on_navigation)
            for child in window.GetChildren():
                self.bind_backgrounds(child)

    def on_background_click(self, event):
        # A child's mouse event must never be mistaken for a background click.
        if isinstance(event.GetEventObject(), (wx.Panel, wx.Frame, wx.StaticBox, wx.StaticText)):
            self.restore_slider_focus = False
            self.neutral.SetFocus()
            # Some native panels re-focus their last child after processing a
            # background mouse press; settle focus after that processing too.
            wx.CallAfter(self.neutral.SetFocus)
            return
        event.Skip()

    def on_navigation(self, event):
        # Native controls process their own arrows. Container navigation is
        # only for Tab/Shift+Tab, never an arrow escaping a control at its edge.
        if event.IsFromTab():
            event.Skip()

    def on_neutral_key(self, event):
        if event.GetKeyCode() == wx.WXK_TAB:
            self.restore_slider_focus = False
            self.neutral.Navigate(wx.NavigationKeyEvent.FromTab |
                                  (wx.NavigationKeyEvent.IsBackward if event.ShiftDown()
                                   else wx.NavigationKeyEvent.IsForward))
        elif event.GetKeyCode() not in ARROWS:
            event.Skip()

    def on_char_hook(self, event):
        focus = wx.Window.FindFocus()
        if focus == self.neutral and event.GetKeyCode() == wx.WXK_TAB:
            self.restore_slider_focus = False
            self.neutral.Navigate(wx.NavigationKeyEvent.FromTab |
                                  (wx.NavigationKeyEvent.IsBackward if event.ShiftDown()
                                   else wx.NavigationKeyEvent.IsForward))
            return
        if (event.GetKeyCode() not in ARROWS or focus is None or
                wx.GetTopLevelParent(focus) != self.frame):
            event.Skip()
            return
        if focus == self.neutral:
            return
        if focus == self.slider:
            self.on_slider_key(event)
            return
        owner = focus
        while owner is not None and owner != self.frame:
            if isinstance(owner, wx.Control):
                # DataView/ComboBox/RadioBox may focus a native child rather
                # than the public control; retain its normal local behavior.
                event.DoAllowNextEvent()
                return
            owner = owner.GetParent()
        # Empty panels do not navigate between controls with arrows.

    def on_slider_key(self, event):
        if event.GetKeyCode() == wx.WXK_TAB:
            self.slider.Navigate(wx.NavigationKeyEvent.FromTab |
                                 (wx.NavigationKeyEvent.IsBackward if event.ShiftDown()
                                  else wx.NavigationKeyEvent.IsForward))
            return
        if event.GetKeyCode() not in ARROWS:
            event.Skip()
            return
        if self.slider.IsEnabled():
            value = max(self.slider.GetMin(), min(self.slider.GetMax(),
                        self.slider.GetValue() + ARROWS[event.GetKeyCode()]))
            if value != self.slider.GetValue():
                self.slider.SetValue(value)
                changed = wx.CommandEvent(wx.EVT_SLIDER.typeId, self.slider.GetId())
                changed.SetEventObject(self.slider)
                changed.SetInt(value)
                self.slider.GetEventHandler().ProcessEvent(changed)
            self.slider.SetFocus()
        # Consume even at an endpoint; no native arrow/focus navigation.

    def enable_slider(self, enabled):
        focus = wx.Window.FindFocus()
        if not enabled and focus == self.slider:
            # Native Disable can otherwise move focus to a selector while the
            # existing debounce/write/read-back is pending. Park it safely.
            self.restore_slider_focus = True
            self.neutral.SetFocus()
        self.slider.Enable(enabled)
        if enabled and self.restore_slider_focus:
            if wx.Window.FindFocus() == self.neutral:
                self.slider.SetFocus()
            self.restore_slider_focus = False
