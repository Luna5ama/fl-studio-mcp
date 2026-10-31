"""Small Windows UI boundary shared by FL-specific native workflows."""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

from fl_studio_mcp.utils.windows_plugin_menu import _find_action


class FLWindows:
    """Operate only windows belonging to the foreground FL Studio process."""

    def __enter__(self):
        if sys.platform != "win32":
            raise RuntimeError("This operation requires Windows")
        from pynput.keyboard import Controller as Keyboard
        from pynput.mouse import Controller as Mouse

        self.user = ctypes.WinDLL("user32", use_last_error=True)
        u = self.user
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.WindowFromPoint.argtypes = [wintypes.POINT]
        u.WindowFromPoint.restype = wintypes.HWND
        u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.SendMessageW.restype = ctypes.c_ssize_t
        u.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        u.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        self._callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.argtypes = [self._callback_type, wintypes.LPARAM]
        u.EnumChildWindows.argtypes = [wintypes.HWND, self._callback_type, wintypes.LPARAM]
        roots = []

        @self._callback_type
        def find_root(hwnd, unused):
            if self.class_name(hwnd) == "TFruityLoopsMainForm" and u.IsWindowVisible(hwnd):
                roots.append(hwnd)
            return True

        u.EnumWindows(find_root, 0)
        if len(roots) != 1:
            raise RuntimeError("Bring FL Studio to the foreground before this operation")
        self.root = roots[0]
        self.process_id = self._pid(self.root)
        self._previous_dpi = u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        self.keyboard = Keyboard()
        self.mouse = Mouse()
        try:
            self.activate()
        except Exception:
            u.SetThreadDpiAwarenessContext(self._previous_dpi)
            raise
        return self

    def __exit__(self, *args):
        self.user.SetThreadDpiAwarenessContext(self._previous_dpi)

    def _pid(self, hwnd):
        value = wintypes.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(value))
        return value.value

    def activate(self):
        """Foreground FL for an explicitly requested native operation."""
        self.user.IsIconic.argtypes = [wintypes.HWND]
        self.user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user.SetForegroundWindow.argtypes = [wintypes.HWND]
        if self.user.IsIconic(self.root):
            self.user.ShowWindow(self.root, 9)
        if self._pid(self.user.GetForegroundWindow()) != self.process_id:
            self.user.SetForegroundWindow(self.root)
        if self._pid(self.user.GetForegroundWindow()) != self.process_id:
            from pynput.keyboard import Key

            # Match the existing Piano Roll activation: Alt permits Windows to
            # foreground FL; always release it before any workflow keystrokes.
            self.keyboard.press(Key.alt)
            try:
                self.user.SetForegroundWindow(self.root)
            finally:
                self.keyboard.release(Key.alt)
            time.sleep(0.1)
        self.require_foreground()

    def require_foreground(self):
        if self._pid(self.user.GetForegroundWindow()) != self.process_id:
            raise RuntimeError("Foreground application changed; inspect FL Studio before retrying")

    def class_name(self, hwnd):
        value = ctypes.create_unicode_buffer(256)
        self.user.GetClassNameW(hwnd, value, len(value))
        return value.value

    def text(self, hwnd):
        if self._pid(hwnd) != self.process_id:
            raise RuntimeError("The window does not belong to FL Studio")
        if "edit" in self.class_name(hwnd).casefold():
            # GetWindowText cannot read another process's Edit control contents.
            result = ctypes.c_size_t()
            send = self.user.SendMessageTimeoutW
            send.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
                wintypes.UINT,
                wintypes.UINT,
                ctypes.POINTER(ctypes.c_size_t),
            ]
            send.restype = ctypes.c_ssize_t
            if not send(hwnd, 0x000E, 0, 0, 2, 1500, ctypes.byref(result)):
                raise RuntimeError("FL text control did not respond")
            value = ctypes.create_unicode_buffer(min(1048576, result.value + 1))
            pointer = ctypes.cast(value, ctypes.c_void_p).value
            if not send(hwnd, 0x000D, len(value), pointer, 2, 1500, ctypes.byref(result)):
                raise RuntimeError("FL text control did not respond")
        else:
            value = ctypes.create_unicode_buffer(max(256, self.user.GetWindowTextLengthW(hwnd) + 1))
            self.user.GetWindowTextW(hwnd, value, len(value))
        return value.value

    def _enumerate(self, parent, class_name, title):
        result = []

        @self._callback_type
        def visit(hwnd, unused):
            if (
                self._pid(hwnd) == self.process_id
                and self.user.IsWindowVisible(hwnd)
                and (class_name is None or self.class_name(hwnd) == class_name)
                and (title is None or self.text(hwnd) == title)
            ):
                result.append(hwnd)
            return True

        if parent is None:
            self.user.EnumWindows(visit, 0)
        else:
            self.user.EnumChildWindows(parent, visit, 0)
        return result

    def windows(self, class_name=None, title=None):
        return self._enumerate(None, class_name, title)

    def children(self, parent=None, class_name=None, title=None):
        return self._enumerate(self.root if parent is None else parent, class_name, title)

    def rect(self, hwnd):
        if self._pid(hwnd) != self.process_id:
            raise RuntimeError("Window ownership changed")
        value = wintypes.RECT()
        if not self.user.GetWindowRect(hwnd, ctypes.byref(value)):
            raise RuntimeError("FL Studio window no longer exists")
        return value.left, value.top, value.right, value.bottom

    def set_text(self, hwnd, text):
        self.require_foreground()
        cls = self.class_name(hwnd).casefold()
        if self._pid(hwnd) != self.process_id or not ("edit" in cls or cls == "tcombobox"):
            raise RuntimeError("Expected an FL Studio text edit control")
        buffer = ctypes.c_wchar_p(str(text))
        result = self.user.SendMessageW(hwnd, 0x000C, 0, ctypes.cast(buffer, ctypes.c_void_p).value)
        if not result:
            raise RuntimeError("FL Studio rejected the text value")

    def click(self, point, button="left", count=1):
        from pynput.mouse import Button

        self.require_foreground()
        point = tuple(round(part) for part in point)
        hit = self.user.WindowFromPoint(wintypes.POINT(*point))
        if self._pid(hit) != self.process_id:
            raise RuntimeError("The requested FL Studio control is covered by another application")
        self.mouse.position = point
        self.require_foreground()
        self.mouse.click(getattr(Button, button), count)
        time.sleep(0.06)

    def press(self, keys):
        from pynput.keyboard import Key

        self.require_foreground()
        aliases = {
            "ctrl": "ctrl",
            "control": "ctrl",
            "alt": "alt",
            "shift": "shift",
            "enter": "enter",
            "return": "enter",
            "esc": "esc",
            "escape": "esc",
            "space": "space",
            "delete": "delete",
            "backspace": "backspace",
            "tab": "tab",
            "home": "home",
            "end": "end",
            "up": "up",
            "down": "down",
            "left": "left",
            "right": "right",
            "pageup": "page_up",
            "pagedown": "page_down",
        }
        parts = keys.split("+") if isinstance(keys, str) else list(keys)
        held = []
        try:
            for name in parts:
                token = str(name).lower().strip()
                value = getattr(Key, aliases.get(token, token), None)
                if value is None:
                    if len(token) != 1:
                        raise ValueError("Unknown keyboard key: " + token)
                    value = token
                self.keyboard.press(value)
                held.append(value)
        finally:
            for value in reversed(held):
                self.keyboard.release(value)
        time.sleep(0.06)

    def type_text(self, text):
        self.require_foreground()
        if "\n" in text or "\r" in text:
            raise ValueError("Send control keys separately from text")
        self.keyboard.type(text)
        time.sleep(0.06)

    def find_text(self, hwnd, text):
        import asyncio

        from PIL import ImageGrab

        self.require_foreground()
        bounds = self.rect(hwnd)
        if not (0 < bounds[2] - bounds[0] <= 10000 and 0 < bounds[3] - bounds[1] <= 10000):
            raise RuntimeError("Unexpected FL window dimensions")
        picture = ImageGrab.grab(bbox=bounds, all_screens=True)
        x, y = asyncio.run(_find_action(picture, text))
        return round(bounds[0] + x), round(bounds[1] + y)

    def _check_point(self, hwnd, point):
        self.user.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
        point = tuple(round(part) for part in point)
        hit = self.user.WindowFromPoint(wintypes.POINT(*point))
        if hit != hwnd and not self.user.IsChild(hwnd, hit):
            raise RuntimeError("The expected FL control is covered by another FL window")

    def click_text(self, hwnd, text):
        point = self.find_text(hwnd, text)
        self._check_point(hwnd, point)
        self.click(point)

    def wait_window(self, class_name=None, title=None, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.require_foreground()
            found = self.windows(class_name=class_name, title=title)
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise RuntimeError("Ambiguous FL Studio dialog")
            time.sleep(0.05)
        raise RuntimeError("Expected FL Studio dialog did not open")

    def choose_menu(self, text, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.require_foreground()
            matches = []
            for hwnd in self.windows(class_name="TQuickPopupMenuWindow"):
                try:
                    matches.append((hwnd, self.find_text(hwnd, text)))
                except RuntimeError:
                    continue
            if len(matches) == 1:
                self._check_point(matches[0][0], matches[0][1])
                self.click(matches[0][1])
                return
            if len(matches) > 1:
                raise RuntimeError("Ambiguous FL Studio menu action: " + text)
            time.sleep(0.05)
        raise RuntimeError("FL Studio menu action not found: " + text)
