"""Recognize and select FL Studio's custom Windows Browser popup menu."""

from __future__ import annotations

import asyncio
import ctypes
import re
import sys
from collections.abc import Callable
from ctypes import wintypes


def _normalize(text: str) -> str:
    text = " ".join(text.replace("&", "").casefold().split())
    # FL draws empty-slot labels in a faint italic font: Windows OCR can read
    # "Slot" as "510t" or "5(ot". Match that word only; keep the slot number exact.
    slot = re.fullmatch(r"[s5][l1i(][o0]t\s*(10|[1-9])", text)
    return "slot " + slot[1] if slot else text


async def _find_action(picture, action: str, leading_icon: bool = False) -> tuple[float, float]:
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    engine = OcrEngine.try_create_from_language(Language("en-US"))
    if engine is None:
        raise RuntimeError("Install the Windows English OCR language to load plugins automatically")
    # FL's menu fonts can be tiny. Enlarge only the menu crop for local Windows OCR.
    scale = min(6, max(1, OcrEngine.max_image_dimension // max(picture.size)))
    picture = picture.convert("RGBA").resize((picture.width * scale, picture.height * scale))
    bitmap = SoftwareBitmap(
        BitmapPixelFormat.BGRA8, picture.width, picture.height, BitmapAlphaMode.IGNORE
    )
    writer = DataWriter()
    writer.write_bytes(picture.tobytes("raw", "BGRA"))
    bitmap.copy_from_buffer(writer.detach_buffer())
    result = await engine.recognize_async(bitmap)
    matches = []
    for line in result.lines:
        words = list(line.words)
        text = line.text
        if leading_icon and len(words) > 1 and words[0].text in ("O", "0", "©", "•", "⚙"):
            words = words[1:]
            text = " ".join(word.text for word in words)
        if _normalize(text) == _normalize(action):
            left = min(word.bounding_rect.x for word in words)
            right = max(word.bounding_rect.x + word.bounding_rect.width for word in words)
            top = min(word.bounding_rect.y for word in words)
            bottom = max(word.bounding_rect.y + word.bounding_rect.height for word in words)
            matches.append(((left + right) / (2 * scale), (top + bottom) / (2 * scale)))
    if len(matches) != 1:
        raise RuntimeError(
            "Could not read the exact Browser menu action (English UI required): "
            + action
            + "; observed: "
            + result.text
        )
    return matches[0]


def select_plugin_menu_action(action: str, before_activate: Callable[[], None]) -> None:
    """Read the owned popup, confirm the FL destination, then click its exact label.

    FL uses a custom TQuickPopupMenuWindow with no accessible menu-item names.
    Windows OCR runs locally on its bounded crop; no screenshots are saved or sent.
    A missing/ambiguous label or foreground change aborts before clicking.
    """
    if sys.platform != "win32":
        raise RuntimeError("Automatic plugin loading currently requires Windows")

    from PIL import ImageGrab
    from pynput.mouse import Button, Controller

    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.WindowFromPoint.argtypes = [wintypes.POINT]
    user.WindowFromPoint.restype = wintypes.HWND
    user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    def process_id(window):
        result = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(result))
        return result.value

    pid = process_id(user.GetForegroundWindow())
    process = kernel.OpenProcess(0x1000, False, pid)
    if not process:
        raise RuntimeError("Cannot inspect the foreground application; leave FL Studio focused")
    try:
        name = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(name))
        if not kernel.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(length)):
            raise RuntimeError("Cannot identify the foreground application")
        executable = name.value.replace("\\", "/").rsplit("/", 1)[-1].casefold()
        if executable not in ("fl.exe", "fl64.exe"):
            raise RuntimeError("FL Studio must stay in the foreground during plugin loading")
    finally:
        kernel.CloseHandle(process)

    candidates = []
    enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @enum_callback
    def visit(window, _):
        caption = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(window, caption, len(caption))
        if (
            caption.value == "TQuickPopupMenuWindow"
            and user.IsWindowVisible(window)
            and process_id(window) == pid
        ):
            candidates.append(window)
        return True

    user.EnumWindows.argtypes = [enum_callback, wintypes.LPARAM]
    user.EnumWindows(visit, 0)
    if len(candidates) != 1:
        raise RuntimeError("Expected exactly one FL Studio popup menu; close other menus and retry")
    window = candidates[0]

    def rectangle():
        bounds = wintypes.RECT()
        if not user.GetWindowRect(window, ctypes.byref(bounds)):
            raise RuntimeError("FL Studio popup menu closed during loading")
        return bounds.left, bounds.top, bounds.right, bounds.bottom

    previous_dpi = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    try:
        bounds = rectangle()
        if not (40 <= bounds[2] - bounds[0] <= 1600 and 20 <= bounds[3] - bounds[1] <= 1600):
            raise RuntimeError("Unexpected FL Studio popup bounds")
        picture = ImageGrab.grab(bbox=bounds, all_screens=True)
        x, y = asyncio.run(_find_action(picture, action))
        point = (round(bounds[0] + x), round(bounds[1] + y))

        def check_target():
            if (
                process_id(user.GetForegroundWindow()) != pid
                or rectangle() != bounds
                or user.WindowFromPoint(wintypes.POINT(*point)) != window
            ):
                raise RuntimeError("FL Studio popup or foreground changed during loading")

        check_target()
        before_activate()
        check_target()
        mouse = Controller()
        mouse.position = point
        check_target()
        mouse.click(Button.left)
    finally:
        user.SetThreadDpiAwarenessContext(previous_dpi)


def drag_effect_to_slot(plugin_name: str, slot: int, before_activate: Callable[[], None]) -> None:
    """Drag a scanned Browser entry onto an OCR-verified empty Mixer slot."""
    import time

    from PIL import ImageEnhance, ImageGrab, ImageOps
    from pynput.mouse import Button, Controller

    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.WindowFromPoint.argtypes = [wintypes.POINT]
    user.WindowFromPoint.restype = wintypes.HWND
    user.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
    user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    root = user.GetForegroundWindow()
    pid = wintypes.DWORD()
    user.GetWindowThreadProcessId(root, ctypes.byref(pid))
    title = ctypes.create_unicode_buffer(256)
    user.GetClassNameW(root, title, len(title))
    if title.value != "TFruityLoopsMainForm":
        raise RuntimeError("FL Studio must stay in the foreground during plugin loading")
    windows = {"TDataBrowserTree": [], "TFXForm": []}
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(window, _):
        caption = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(window, caption, len(caption))
        if caption.value in windows and user.IsWindowVisible(window):
            windows[caption.value].append(window)
        return True

    user.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
    user.EnumChildWindows(root, visit, 0)
    if any(len(value) != 1 for value in windows.values()):
        raise RuntimeError("Keep one Browser and the target Mixer visible during effect loading")
    browser = windows["TDataBrowserTree"][0]
    mixer = windows["TFXForm"][0]

    def rectangle(window):
        value = wintypes.RECT()
        if not user.GetWindowRect(window, ctypes.byref(value)):
            raise RuntimeError("FL Studio window closed during loading")
        return value.left, value.top, value.right, value.bottom

    previous_dpi = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    try:
        browser_rect, mixer_rect = rectangle(browser), rectangle(mixer)
        source_image = ImageGrab.grab(bbox=browser_rect, all_screens=True)
        try:
            sx, sy = asyncio.run(_find_action(source_image, plugin_name, leading_icon=True))
        except RuntimeError:
            sx, sy = asyncio.run(
                _find_action(source_image, plugin_name + ".fst", leading_icon=True)
            )
        source = (round(browser_rect[0] + sx), round(browser_rect[1] + sy))
        width = min(330, (mixer_rect[2] - mixer_rect[0]) // 2)
        destinations = []
        errors = []
        for left in (mixer_rect[0], mixer_rect[2] - width):
            crop = (left, mixer_rect[1], left + width, min(mixer_rect[3], mixer_rect[1] + 750))
            try:
                picture = ImageGrab.grab(bbox=crop, all_screens=True)
                picture = ImageEnhance.Contrast(ImageOps.grayscale(picture)).enhance(3)
                x, y = asyncio.run(_find_action(picture, f"Slot {slot + 1}"))
                destinations.append((round(left + x), round(crop[1] + y)))
            except RuntimeError as exc:
                errors.append(str(exc))
                continue
        if len(destinations) != 1:
            raise RuntimeError(
                "Cannot read the empty destination slot; keep the Mixer inspector visible; "
                + "; ".join(errors)
            )
        target = destinations[0]

        def check_target():
            if (
                user.GetForegroundWindow() != root
                or rectangle(browser) != browser_rect
                or rectangle(mixer) != mixer_rect
            ):
                raise RuntimeError("FL Studio focus or layout changed during effect loading")
            for point, owner in ((source, browser), (target, mixer)):
                hit = user.WindowFromPoint(wintypes.POINT(*point))
                if hit != owner and not user.IsChild(owner, hit):
                    raise RuntimeError("Browser or effect slot is covered; keep both visible")

        check_target()
        before_activate()
        check_target()
        mouse = Controller()
        mouse.position = source
        mouse.press(Button.left)
        try:
            for step in range(1, 21):
                mouse.position = (
                    round(source[0] + (target[0] - source[0]) * step / 20),
                    round(source[1] + (target[1] - source[1]) * step / 20),
                )
                time.sleep(0.02)
        finally:
            mouse.release(Button.left)
    finally:
        user.SetThreadDpiAwarenessContext(previous_dpi)
