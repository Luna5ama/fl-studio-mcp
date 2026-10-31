"""Windows native file operations with disk-based completion checks.

Image-Line documents context-dependent Save As/Ctrl+R shortcuts. The caller must
first focus Playlist through the MIDI controller; sending keys is not completion.
"""

from __future__ import annotations

import ctypes
import os
import re
import struct
import time
import uuid
from pathlib import Path

from fl_studio_mcp.utils.windows_ui import FLWindows

AUDIO_SUFFIXES = {".wav", ".wave", ".aif", ".aiff", ".flac", ".mp3", ".ogg"}


def input_audio_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or any(character in value for character in "\r\n\x00"):
        raise ValueError("An absolute audio file path is required")
    path = path.resolve(strict=True)
    if not path.is_file() or path.suffix.casefold() not in AUDIO_SUFFIXES:
        raise ValueError("Expected an existing WAV, AIFF, FLAC, MP3 or OGG audio file")
    return path


def output_path(value: str, suffix: str, overwrite: bool = False) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or any(character in value for character in "\r\n\x00"):
        raise ValueError("An explicit absolute output path is required")
    path = path.resolve(strict=False)
    if path.suffix.casefold() != suffix:
        raise ValueError("Output filename must end in " + suffix)
    if not path.parent.is_dir():
        raise ValueError("The output parent directory must already exist")
    if path.exists() and (not path.is_file() or not overwrite):
        raise FileExistsError("Output already exists; pass overwrite=True explicitly: " + str(path))
    return path


def inspect_flp(path: Path) -> dict:
    size = path.stat().st_size
    with path.open("rb") as stream:
        head = stream.read(14)
        if len(head) != 14 or head[:4] != b"FLhd" or struct.unpack("<I", head[4:8])[0] != 6:
            raise ValueError("Output is not a complete FL Studio FLP header")
        format_id, channel_count, ppq = struct.unpack("<HHH", head[8:14])
        if format_id != 0 or ppq == 0:
            raise ValueError("Unexpected FLP format or timing header")
        data_head = stream.read(8)
        if len(data_head) != 8 or data_head[:4] != b"FLdt":
            raise ValueError("FLP has no event-data chunk")
        declared = struct.unpack("<I", data_head[4:])[0]
        if declared == 0 or 22 + declared > size:
            raise ValueError("FLP event-data chunk is incomplete")
    return {"format": "flp", "bytes": size, "header_channel_count": channel_count, "ppq": ppq}


def inspect_wav(path: Path) -> dict:
    size = path.stat().st_size
    with path.open("rb") as stream:
        header = stream.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise ValueError("Expected a standard RIFF/WAVE output")
        declared_size = struct.unpack("<I", header[4:8])[0] + 8
        if declared_size != size:
            raise ValueError("WAV RIFF length does not match the completed file")
        fmt = None
        data_bytes = 0
        position = 12
        while position + 8 <= size:
            stream.seek(position)
            kind, length = struct.unpack("<4sI", stream.read(8))
            end = position + 8 + length
            if end > size:
                raise ValueError("WAV contains an incomplete chunk")
            if kind == b"fmt ":
                raw = stream.read(min(length, 64))
                if len(raw) < 16:
                    raise ValueError("WAV format chunk is incomplete")
                tag, channels, rate, byte_rate, alignment, bits = struct.unpack("<HHIIHH", raw[:16])
                valid_bits = bits
                if tag == 0xFFFE:
                    if len(raw) < 40:
                        raise ValueError("WAV extensible format chunk is incomplete")
                    valid_bits = struct.unpack("<H", raw[18:20])[0]
                    tag = struct.unpack("<H", raw[24:26])[0]
                if tag not in (1, 3) or channels < 1 or rate < 1 or alignment < 1:
                    raise ValueError("Unsupported or invalid WAV sample format")
                if bits not in (8, 16, 24, 32, 64) or alignment != channels * bits // 8:
                    raise ValueError("Invalid WAV sample alignment")
                if byte_rate != rate * alignment:
                    raise ValueError("Invalid WAV byte rate")
                fmt = (tag, channels, rate, alignment, bits, valid_bits)
            elif kind == b"data":
                data_bytes += length
            position = end + (length & 1)
        if position != size:
            raise ValueError("WAV contains an incomplete chunk header or padding")
        if fmt is None or data_bytes == 0 or data_bytes % fmt[3]:
            raise ValueError("WAV contains no complete audio frames")
    frames = data_bytes // fmt[3]
    return {
        "format": "wav",
        "bytes": size,
        "audio_format": "pcm" if fmt[0] == 1 else "float",
        "channels": fmt[1],
        "sample_rate": fmt[2],
        "bits_per_sample": fmt[4],
        "valid_bits_per_sample": fmt[5],
        "frames": frames,
        "duration_seconds": frames / fmt[2],
    }


def _stable_file(path, inspect, deadline, *, newer_than=None, complete=None):
    previous = None
    stable_since = None
    last_error = "Output has not appeared"
    while time.monotonic() < deadline:
        try:
            value = path.stat()
            fingerprint = (value.st_size, value.st_mtime_ns)
            if newer_than is not None and fingerprint == newer_than:
                last_error = "Existing output has not been updated"
            elif fingerprint == previous:
                if stable_since is not None and time.monotonic() - stable_since >= 0.4:
                    result = inspect(path)
                    if complete is None or complete():
                        return result
            else:
                previous, stable_since = fingerprint, time.monotonic()
            last_error = "Output is still being written or its dialog is active"
        except (OSError, ValueError) as exc:
            last_error = str(exc)
        time.sleep(0.1)
    raise TimeoutError("Output completion could not be verified: " + last_error)


def _file_dialog(windows, existing, deadline):
    while time.monotonic() < deadline:
        windows.require_foreground()
        candidates = []
        for hwnd in windows.windows():
            if hwnd in existing or hwnd == windows.root:
                continue
            cls = windows.class_name(hwnd)
            title = windows.text(hwnd).casefold()
            if cls == "#32770":
                # Windows file dialogs follow the OS locale, even when FL uses
                # English. Identify their filename field and IDOK, not the title.
                try:
                    _filename_edit(windows, hwnd)
                    if _dialog_button(windows, hwnd, 1) is not None:
                        candidates.append(hwnd)
                except RuntimeError:
                    continue
            elif ("file" in cls.casefold() or "dialog" in cls.casefold()) and any(
                word in title for word in ("save", "open", "sample", "export", "render")
            ):
                candidates.append(hwnd)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise RuntimeError("More than one new FL Studio file dialog is open")
        time.sleep(0.05)
    raise RuntimeError("Expected FL Studio file dialog did not open")


def _control_id(windows, hwnd):
    windows.user.GetDlgCtrlID.argtypes = [ctypes.c_void_p]
    windows.user.GetDlgCtrlID.restype = ctypes.c_int
    return windows.user.GetDlgCtrlID(hwnd)


def _uia_automation_id(hwnd, expected_pid=None):
    """Read one native element property through Windows' IUIAutomation COM API."""
    ole = ctypes.WinDLL("ole32")
    automation = ctypes.WinDLL("oleaut32")
    pointer = ctypes.c_void_p
    guid_type = ctypes.c_ubyte * 16
    ole.CoInitializeEx.argtypes = [pointer, ctypes.c_ulong]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoCreateInstance.argtypes = [
        ctypes.POINTER(guid_type),
        pointer,
        ctypes.c_ulong,
        ctypes.POINTER(guid_type),
        ctypes.POINTER(pointer),
    ]
    ole.CoCreateInstance.restype = ctypes.c_long
    automation.SysFreeString.argtypes = [pointer]
    initialized = ole.CoInitializeEx(None, 0)
    if initialized < 0 and initialized != -2147417850:  # RPC_E_CHANGED_MODE uses existing COM.
        raise RuntimeError("Windows UI Automation initialization failed")
    client, element, value = pointer(), pointer(), pointer()

    def method(instance, slot, result_type, *arguments):
        table = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(pointer))).contents
        return ctypes.WINFUNCTYPE(result_type, pointer, *arguments)(table[slot])

    try:
        clsid = guid_type.from_buffer_copy(
            uuid.UUID("ff48dba4-60ef-4201-aa87-54103eef594e").bytes_le
        )
        iid = guid_type.from_buffer_copy(uuid.UUID("30cbe57d-d9d0-452a-ab13-7ac5ac4825ee").bytes_le)
        if (
            ole.CoCreateInstance(
                ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(client)
            )
            < 0
        ):
            raise RuntimeError("Windows UI Automation client is unavailable")
        # IUIAutomation::ElementFromHandle, IUIAutomationElement::get_CurrentAutomationId.
        if (
            method(client, 6, ctypes.c_long, pointer, ctypes.POINTER(pointer))(
                client, hwnd, ctypes.byref(element)
            )
            < 0
        ):
            raise RuntimeError("Cannot inspect the native confirmation control")
        actual_pid = ctypes.c_int()
        if (
            method(element, 20, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
                element, ctypes.byref(actual_pid)
            )
            < 0
        ):
            raise RuntimeError("Cannot verify the native confirmation process")
        if expected_pid is not None and actual_pid.value != expected_pid:
            raise RuntimeError("Native confirmation process identity changed")
        if (
            method(element, 29, ctypes.c_long, ctypes.POINTER(pointer))(
                element, ctypes.byref(value)
            )
            < 0
        ):
            raise RuntimeError("Cannot read the native confirmation control identity")
        return ctypes.wstring_at(value) if value else ""
    finally:
        if value:
            automation.SysFreeString(value)
        for instance in (element, client):
            if instance:
                method(instance, 2, ctypes.c_ulong)(instance)
        if initialized >= 0:
            ole.CoUninitialize()


def _dialog_button(windows, dialog, control_id):
    matches = []
    for hwnd in windows.children(dialog):
        if windows.class_name(hwnd).casefold() != "button":
            continue
        native_id = _control_id(windows, hwnd)
        if native_id == control_id or (
            native_id == 0
            and control_id in (6, 7)
            and _uia_automation_id(hwnd, windows.process_id) == "CommandButton_" + str(control_id)
        ):
            matches.append(hwnd)
    if len(matches) > 1:
        raise RuntimeError("Ambiguous standard file-dialog button")
    return matches[0] if matches else None


def _filename_edit(windows, dialog):
    edits = [
        hwnd
        for hwnd in windows.children(dialog)
        if windows.class_name(hwnd).casefold() in ("edit", "tedit")
    ]
    preferred = [hwnd for hwnd in edits if _control_id(windows, hwnd) in (1001, 1152, 1148)]
    if len(preferred) == 1:
        return preferred[0]
    if len(edits) == 1:
        return edits[0]
    raise RuntimeError("Cannot identify the File name field in the FL Studio file dialog")


def _submit_filename(windows, dialog, path, button):
    edit = _filename_edit(windows, dialog)
    windows.set_text(edit, str(path))
    if windows.text(edit) != str(path):
        raise RuntimeError("File dialog did not accept the exact requested pathname")
    standard = (
        _dialog_button(windows, dialog, 1) if windows.class_name(dialog) == "#32770" else None
    )
    buttons = (
        [standard]
        if standard is not None
        else [
            hwnd
            for hwnd in windows.children(dialog)
            if windows.text(hwnd).replace("&", "").strip().casefold() == button.casefold()
        ]
    )
    if len(buttons) != 1:
        raise RuntimeError("Cannot identify the file dialog " + button + " button")
    left, top, right, bottom = windows.rect(buttons[0])
    windows.click(((left + right) / 2, (top + bottom) / 2))


def _confirm_overwrite(windows, path, excluded, save_dialog=None):
    matches = []
    filename = re.compile(r"(?<![\w.-])" + re.escape(path.name) + r"(?![\w.-])", re.IGNORECASE)
    for hwnd in windows.windows(class_name="#32770"):
        if hwnd in excluded:
            continue
        children = windows.children(hwnd)
        message = " ".join(
            windows.text(child)
            for child in children
            if windows.class_name(child).casefold() == "static"
        )
        if not message:
            message = " ".join(windows.text(child) for child in children)
        if not filename.search(message):
            from fl_studio_mcp.utils.arrangement_windows import _scan

            message = " ".join(record["text"] for record in _scan(windows, hwnd))
        if not filename.search(message):
            continue
        yes = _dialog_button(windows, hwnd, 6)  # IDYES is independent of OS locale.
        no = _dialog_button(windows, hwnd, 7)
        if yes is not None and no is not None:
            matches.append((hwnd, yes))
    if len(matches) > 1:
        raise RuntimeError("More than one matching overwrite confirmation is open")
    if not matches:
        return False
    hwnd, yes = matches[0]
    left, top, right, bottom = windows.rect(yes)
    point = ((left + right) / 2, (top + bottom) / 2)
    windows._check_point(hwnd, point)
    windows.click(point)
    return True


def save_project_file(path: Path, overwrite: bool, timeout: float) -> dict:
    started = time.monotonic()
    before = None
    if path.exists():
        value = path.stat()
        before = value.st_size, value.st_mtime_ns
    with FLWindows() as windows:
        existing = set(windows.windows())
        windows.press("ctrl+shift+s")
        dialog = _file_dialog(windows, existing, min(started + timeout, time.monotonic() + 8))
        if path.exists() and not overwrite:
            raise FileExistsError(
                "Destination appeared while Save As was open; nothing overwritten"
            )
        _submit_filename(windows, dialog, path, "Save")
        if overwrite:
            deadline = min(started + timeout, time.monotonic() + 2)
            while time.monotonic() < deadline:
                if _confirm_overwrite(windows, path, existing | {dialog}, save_dialog=dialog):
                    break
                if dialog not in windows.windows() and path.exists():
                    current = path.stat()
                    if (current.st_size, current.st_mtime_ns) != before:
                        break
                time.sleep(0.05)
        details = _stable_file(
            path,
            inspect_flp,
            started + timeout,
            newer_than=before,
            complete=lambda: dialog not in windows.windows(),
        )
    details.update(path=str(path), status="saved", overwrite=overwrite)
    return details


def _render_window(windows, existing, deadline):
    while time.monotonic() < deadline:
        matches = []
        for hwnd in windows.windows():
            if hwnd == windows.root or hwnd in existing:
                continue
            if windows.class_name(hwnd) == "TWAVRenderForm":
                matches.append(hwnd)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RuntimeError("More than one render dialog is open")
        time.sleep(0.05)
    raise RuntimeError("FL Studio render options did not open")


def _label_controls(windows, dialog, label):
    return [
        hwnd
        for hwnd in windows.children(dialog)
        if windows.text(hwnd).replace("&", "").strip().casefold() == label.casefold()
    ]


def _render_button_point(windows, dialog, label):
    from fl_studio_mcp.utils.arrangement_windows import _scan

    left, top, right, bottom = windows.rect(dialog)
    records = _scan(windows, dialog, (left, max(top, bottom - 85), right, bottom))
    words = [
        word
        for record in records
        for word in record["words"]
        if word["text"].casefold() == label.casefold()
    ]
    if len(words) != 1:
        raise RuntimeError("Cannot identify the render " + label + " button uniquely")
    left, top, right, bottom = words[0]["rect"]
    return ((left + right) / 2, (top + bottom) / 2)


def _has_label(windows, dialog, label):
    if _label_controls(windows, dialog, label):
        return True
    try:
        if label in ("Start", "Abort"):
            _render_button_point(windows, dialog, label)
        else:
            windows.find_text(dialog, label)
        return True
    except RuntimeError:
        return False


def _click_label(windows, dialog, label):
    controls = _label_controls(windows, dialog, label)
    if len(controls) == 1:
        left, top, right, bottom = windows.rect(controls[0])
        windows.click(((left + right) / 2, (top + bottom) / 2))
    elif controls:
        raise RuntimeError("Ambiguous render control: " + label)
    else:
        if label in ("Start", "Abort"):
            point = _render_button_point(windows, dialog, label)
            windows._check_point(dialog, point)
            windows.click(point)
        else:
            windows.click_text(dialog, label)


def _render_complete(windows, dialog):
    if dialog not in windows.windows():
        return True
    return not _has_label(windows, dialog, "Abort") and _has_label(windows, dialog, "Start")


def _render_mode(windows, dialog):
    from fl_studio_mcp.utils.arrangement_windows import _scan

    panels = windows.children(dialog, class_name="TVectorPanel", title="Project type")
    if len(panels) != 1:
        raise RuntimeError("Cannot identify the render Project type panel")
    choices = []
    for record in _scan(windows, panels[0]):
        words = record["words"]
        tokens = [word["text"].casefold().rstrip(":") for word in words]
        if "mode" not in tokens:
            continue
        index = tokens.index("mode") + 1
        if tokens[index : index + 1] == ["pattern"]:
            mode, count = "Pattern", 1
        elif tokens[index : index + 2] == ["full", "song"]:
            mode, count = "Full song", 2
        elif tokens[index : index + 2] == ["song", "selection"]:
            mode, count = "Song selection", 2
        else:
            continue
        value = words[index : index + count]
        point = (
            (value[0]["rect"][0] + value[-1]["rect"][2]) / 2,
            (value[0]["rect"][1] + value[0]["rect"][3]) / 2,
        )
        choices.append((mode, point))
    if len(choices) != 1:
        raise RuntimeError("Cannot read the exact render Mode value")
    return choices[0]


def _set_render_scope(windows, dialog, scope):
    expected = {
        "pattern": "Pattern",
        "full_song": "Full song",
        "current_selection": "Song selection",
    }[scope]
    actual, point = _render_mode(windows, dialog)
    if actual == expected:
        return expected
    windows._check_point(dialog, point)
    windows.click(point)
    windows.choose_menu(expected)
    if _render_mode(windows, dialog)[0] != expected:
        raise RuntimeError("FL Studio did not apply the requested render mode")
    return expected


def _publish_file(staged, destination, overwrite):
    # Both paths are on the same filesystem. The final path must never contain a
    # partial copy, including when a competing writer creates it during rendering.
    if overwrite:
        os.replace(staged, destination)
    elif os.name == "nt":
        os.rename(staged, destination)  # Windows rename refuses existing targets.
    else:
        os.link(staged, destination)  # Atomic exclusive publication for offline tests.
        staged.unlink()


def render_wav_file(conn, path: Path, overwrite: bool, timeout: float, scope: str) -> dict:
    started = time.monotonic()
    staging = path.parent / (".mcp-render-" + uuid.uuid4().hex)
    staging.mkdir()
    staged = staging / "render.wav"
    with FLWindows() as windows:
        if scope == "full_song":
            from fl_studio_mcp.utils.arrangement_windows import clear_time_selection

            clear_time_selection(conn, windows=windows)
        existing = set(windows.windows())
        windows.press("ctrl+r")
        dialog = _file_dialog(windows, existing, min(started + timeout, time.monotonic() + 8))
        _submit_filename(windows, dialog, staged, "Save")
        options = _render_window(
            windows, existing | {dialog}, min(started + timeout, time.monotonic() + 10)
        )
        render_mode = _set_render_scope(windows, options, scope)
        _click_label(windows, options, "Start")
        details = _stable_file(
            staged,
            inspect_wav,
            started + timeout,
            complete=lambda: _render_complete(windows, options),
        )
        if options in windows.windows():
            windows.press("esc")
    _publish_file(staged, path, overwrite)
    published = inspect_wav(path)
    if published != details:
        raise RuntimeError("Published WAV does not match the verified rendered output")
    details.update(
        path=str(path),
        status="rendered",
        render_scope=scope,
        render_mode=render_mode,
        staging_directory=str(staging),
        overwrite=overwrite,
        notes=[
            "Actual sample rate follows FL Audio Settings.",
            "Additional selected formats or split stems remain inside staging.",
        ],
    )
    return details


def _open_rack_menu(windows):
    racks = windows.children(class_name="TStepSeqForm", title="Channel rack")
    if len(racks) != 1:
        raise RuntimeError("Keep one unobstructed Channel Rack visible")
    captions = windows.children(racks[0], class_name="TNewCaption", title="Channel rack")
    if len(captions) != 1:
        raise RuntimeError("Cannot identify the Channel Rack menu caption")
    left, top, right, bottom = windows.rect(captions[0])
    if right - left < 80 or not 12 <= bottom - top <= 80:
        raise RuntimeError("Unexpected Channel Rack caption geometry")
    point = (left + 10, (top + bottom) / 2)
    windows._check_point(captions[0], point)
    windows.click(point)


def _sample_menu_action(windows, label, *, hover=False):
    # EnumWindows yields the active nested popup before its parent. Repeated
    # folder/plugin names (Sampler > Sampler) must resolve in that order.
    for menu in windows.windows(class_name="TQuickPopupMenuWindow"):
        point = None
        for text in (label, label + " >"):
            try:
                point = windows.find_text(menu, text)
                break
            except RuntimeError:
                windows.require_foreground()
                continue
        if point is None:
            continue
        windows._check_point(menu, point)
        if hover:
            windows.require_foreground()
            windows.mouse.position = point
            windows.require_foreground()
            time.sleep(0.2)
        else:
            windows.click(point)
        return True
    return False


def _open_sample_channel_menu(conn, windows, index, before_channels, type_id, initial_name):
    checked = conn.send_command(
        "projectFiles.focusChannel",
        {
            "index": index,
            "channel_type": type_id,
            "before_channels": before_channels,
        },
    )
    if not checked.get("success") or checked.get("channel", {}).get("name") != initial_name:
        raise RuntimeError("Sample-channel identity changed before its context menu")
    _wait_focus(conn, "rack", time.monotonic() + 3)
    racks = windows.children(class_name="TStepSeqForm", title="Channel rack")
    if len(racks) != 1:
        raise RuntimeError("Keep one unobstructed Channel Rack visible")
    if any(record["name"].casefold() == initial_name.casefold() for record in before_channels):
        raise RuntimeError("The new sample channel label is ambiguous; rename existing duplicates")
    from fl_studio_mcp.utils.arrangement_windows import _scan

    bars = windows.children(racks[0], class_name="TNumBar")
    if len(bars) != 1:
        raise RuntimeError("Keep the Channel Rack horizontal scrollbar visible")
    left, top, right, bottom = windows.rect(racks[0])
    names = (left + 100, top + 30, windows.rect(bars[0])[0] - 12, bottom - 30)
    matches = [
        record
        for record in _scan(windows, racks[0], names)
        if " ".join(record["text"].casefold().split()) == " ".join(initial_name.casefold().split())
    ]
    if len(matches) != 1:
        raise RuntimeError("Cannot read the new channel's complete name button uniquely")
    left, top, right, bottom = matches[0]["rect"]
    point = ((left + right) / 2, (top + bottom) / 2)
    windows._check_point(racks[0], point)
    windows.click(point, button="right")


def _choose_load_sample(windows, timeout=4):
    from fl_studio_mcp.utils.arrangement_windows import _scan

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = []
        for menu in windows.windows(class_name="TQuickPopupMenuWindow"):
            try:
                records = _scan(windows, menu)
            except RuntimeError:
                windows.require_foreground()
                if menu not in windows.windows(class_name="TQuickPopupMenuWindow"):
                    continue
                raise
            for record in records:
                if re.fullmatch(
                    r"[li1]-?oad\s+sample[.\u2026\u201e,\s]*", record["text"].casefold()
                ):
                    left, top, right, bottom = record["rect"]
                    matches.append((menu, ((left + right) / 2, (top + bottom) / 2)))
        if len(matches) > 1:
            raise RuntimeError("Ambiguous Load sample menu action")
        if matches:
            windows._check_point(*matches[0])
            windows.click(matches[0][1])
            return
        time.sleep(0.1)
    raise RuntimeError("Cannot read the exact Load sample action in the channel context menu")


def create_sample_channel(
    conn, source: Path, channel_type: str, channel_name, mixer_track, timeout: float
) -> dict:
    deadline = time.monotonic() + timeout
    type_id = 0 if channel_type == "sampler" else 4
    with FLWindows() as windows:
        response = conn.send_command("projectFiles.prepare", {"target": "rack"})
        if not response.get("success"):
            return response
        before = response["channel_count"]
        before_channels = response["channels"]
        _wait_focus(conn, "rack", min(deadline, time.monotonic() + 5))
        _open_rack_menu(windows)
        windows.choose_menu("Add one")
        menu_steps = 0
        while time.monotonic() < deadline:
            response = conn.send_command(
                "projectFiles.inspectNewChannel",
                {
                    "before_count": before,
                    "before_channels": before_channels,
                    "channel_type": type_id,
                },
            )
            if not response.get("success"):
                return response
            if response.get("status") == "created":
                break
            if response.get("status") != "waiting":
                raise RuntimeError(
                    "Unexpected Channel Rack changes while creating sample destination"
                )
            if windows.windows(class_name="TQuickPopupMenuWindow"):
                if menu_steps >= 8:
                    raise RuntimeError("Sample plugin menu nesting exceeded the supported depth")
                selected = _sample_menu_action(windows, "Sampler" if type_id == 0 else "Audio Clip")
                if not selected and type_id == 4:
                    # Hovering a category expands it without activating a different
                    # instrument if Audio Clip is absent from the user's database.
                    selected = _sample_menu_action(windows, "Misc", hover=True)
                if selected:
                    menu_steps += 1
            time.sleep(0.1)
        else:
            raise TimeoutError("New sample destination was not created")
        index = response["channel"]["index"]
        initial_name = response["channel"]["name"]
        if initial_name.casefold() == source.stem.casefold():
            raise RuntimeError(
                "Sample filename matches the empty destination label; "
                "rename the source file before importing"
            )
        response = conn.send_command(
            "projectFiles.focusChannel",
            {"index": index, "channel_type": type_id, "before_channels": before_channels},
        )
        if not response.get("success"):
            return response
        _wait_focus(conn, "rack", min(deadline, time.monotonic() + 5))
        existing = set(windows.windows())
        _open_sample_channel_menu(conn, windows, index, before_channels, type_id, initial_name)
        _choose_load_sample(windows)
        dialog = _file_dialog(windows, existing, min(deadline, time.monotonic() + 8))
        _submit_filename(windows, dialog, source, "Open")
        while time.monotonic() < deadline:
            if dialog in windows.windows():
                time.sleep(0.1)
                continue
            response = conn.send_command(
                "projectFiles.finishImport",
                {
                    "index": index,
                    "before_count": before,
                    "channel_type": type_id,
                    "expected_stem": source.stem,
                    "initial_name": initial_name,
                    "before_channels": before_channels,
                    "mixer_track": mixer_track,
                    "channel_name": channel_name,
                },
            )
            if not response.get("success") or response.get("status") == "imported":
                response["source_path"] = str(source)
                return response
            time.sleep(0.1)
    raise TimeoutError("Sample loading was not verified; inspect the newly created channel")


def _wait_focus(conn, target, deadline):
    key = "playlist_focused" if target == "playlist" else "rack_focused"
    stable_since = None
    while time.monotonic() < deadline:
        response = conn.send_command("projectFiles.status")
        if not response.get("success"):
            raise RuntimeError(response.get("error", "FL status unavailable"))
        if response.get(key):
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= 0.2:
                return response
        else:
            stable_since = None
        time.sleep(0.05)
    raise RuntimeError("FL Studio did not focus the requested file-operation context")


def prepare_playlist(conn, stop_playback=False):
    with FLWindows():
        response = conn.send_command(
            "projectFiles.prepare", {"target": "playlist", "stop_playback": stop_playback}
        )
        if not response.get("success"):
            raise RuntimeError(response.get("error", "Playlist preparation failed"))
        return _wait_focus(conn, "playlist", time.monotonic() + 5)
