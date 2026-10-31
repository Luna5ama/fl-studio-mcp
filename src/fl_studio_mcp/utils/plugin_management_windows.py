"""Windows operations for FL plugin lifecycle; every edit has host readback."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import re
import struct
import time
import unicodedata
import uuid
from pathlib import Path

from fl_studio_mcp.utils.runtime_operations import preset_operations
from fl_studio_mcp.utils.windows_plugin_menu import _normalize
from fl_studio_mcp.utils.windows_ui import FLWindows

FULL_STATE_PARAMETER_LIMIT = 16384


def call(conn, command, params):
    response = conn.send_command("pluginManagement." + command, params)
    if not response.get("success"):
        raise RuntimeError(response.get("error", "FL plugin operation failed"))
    return response


def identity(item):
    result = {
        key: item.get(key)
        for key in (
            "occupied",
            "plugin_name",
            "display_name",
            "parameter_count",
            "parameter_fingerprint",
            "mix_level",
        )
    }

    result["parameter_fingerprint"] = item.get(
        "named_parameter_fingerprint", item.get("parameter_fingerprint")
    )
    return result


def validate_fst(path):
    data = Path(path).read_bytes()
    if len(data) < 22 or data[:4] != b"FLhd":
        raise ValueError("Expected a complete FL Studio FST file")
    header_size = struct.unpack_from("<I", data, 4)[0]
    offset = 8 + header_size
    if header_size < 6 or offset + 8 > len(data) or data[offset : offset + 4] != b"FLdt":
        raise ValueError("Invalid FST chunk layout")
    length = struct.unpack_from("<I", data, offset + 4)[0]
    if offset + 8 + length != len(data):
        raise ValueError("Truncated or unsupported FST data")
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def wait_fst(path, timeout):
    deadline = time.monotonic() + timeout
    previous = None
    stable = 0
    while time.monotonic() < deadline:
        try:
            stat = path.stat()
            state = (stat.st_size, stat.st_mtime_ns)
            stable = stable + 1 if state == previous else 0
            previous = state
            if stable >= 3:
                return validate_fst(path)
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise RuntimeError("FST file was not completely saved before the deadline")


async def _ocr_lines(picture):
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    engine = OcrEngine.try_create_from_language(Language("en-US"))
    if engine is None:
        raise RuntimeError("Windows English OCR is required for native plugin controls")
    scale = min(5, max(1, OcrEngine.max_image_dimension // max(picture.size)))
    image = picture.convert("RGBA").resize((picture.width * scale, picture.height * scale))
    bitmap = SoftwareBitmap(
        BitmapPixelFormat.BGRA8, image.width, image.height, BitmapAlphaMode.IGNORE
    )
    writer = DataWriter()
    writer.write_bytes(image.tobytes("raw", "BGRA"))
    bitmap.copy_from_buffer(writer.detach_buffer())
    result = await engine.recognize_async(bitmap)
    rows = []
    for line in result.lines:
        words = list(line.words)
        if not words:
            continue
        left = min(word.bounding_rect.x for word in words) / scale
        top = min(word.bounding_rect.y for word in words) / scale
        right = max(word.bounding_rect.x + word.bounding_rect.width for word in words) / scale
        bottom = max(word.bounding_rect.y + word.bounding_rect.height for word in words) / scale
        text = line.text
        if len(words) > 1 and words[0].text in ("O", "0", "©", "•", "⚙", ">"):
            text = " ".join(word.text for word in words[1:])
        rows.append({"text": text, "box": (left, top, right, bottom)})
    return rows


def _slot_layout(windows, slots, slot):
    from PIL import ImageEnhance, ImageGrab, ImageOps

    mixers = windows.children(class_name="TFXForm")
    if len(mixers) != 1:
        raise RuntimeError("Keep exactly one Mixer and its inspector visible")
    mixer = mixers[0]
    bounds = windows.rect(mixer)
    width = min(360, (bounds[2] - bounds[0]) // 2)
    expected = [
        _normalize(item.get("display_name") or item.get("plugin_name") or f"Slot {n + 1}")
        for n, item in enumerate(slots)
    ]
    candidates = []
    for left in (bounds[0], bounds[2] - width):
        crop = (left, bounds[1], left + width, min(bounds[3], bounds[1] + 820))
        picture = ImageGrab.grab(bbox=crop, all_screens=True)
        enhanced = ImageEnhance.Contrast(ImageOps.grayscale(picture)).enhance(3)
        rows = asyncio.run(_ocr_lines(enhanced))
        matches = []
        for index, label in enumerate(expected):
            found = [row for row in rows if _normalize(row["text"]) == label]
            if expected.count(label) == 1 and len(found) == 1:
                row = found[0]
                matches.append((index, (row["box"][1] + row["box"][3]) / 2))
        if len(matches) >= 2:
            slopes = [
                (b[1] - a[1]) / (b[0] - a[0]) for a in matches for b in matches if b[0] > a[0]
            ]
            slopes.sort()
            pitch = slopes[len(slopes) // 2]
            intercepts = sorted(y - n * pitch for n, y in matches)
            first = intercepts[len(intercepts) // 2]
            if not 14 <= pitch <= 90 or any(abs(y - (first + n * pitch)) > 4 for n, y in matches):
                continue
            y = first + slot * pitch
            found = [
                row
                for row in rows
                if _normalize(row["text"]) == expected[slot]
                and abs((row["box"][1] + row["box"][3]) / 2 - y) <= 4
            ]
            if not found and crop[2] == bounds[2]:
                # A long slot name can be merged with the adjacent round wet
                # knob as a trailing 'O'. Re-OCR only this independently fitted
                # row's text column, never strip characters from a plugin name.
                row_top = max(0, round(y - pitch * 0.45))
                row_bottom = min(enhanced.height, round(y + pitch * 0.45))
                text_right = max(1, round(enhanced.width - pitch * 1.35))
                narrow = enhanced.crop((0, row_top, text_right, row_bottom))
                for candidate in asyncio.run(_ocr_lines(narrow)):
                    if _normalize(candidate["text"]) == expected[slot]:
                        box = candidate["box"]
                        found.append(
                            {
                                "text": candidate["text"],
                                "box": (box[0], box[1] + row_top, box[2], box[3] + row_top),
                            }
                        )
            if len(found) == 1:
                row = found[0]
                candidates.append(
                    {
                        "window": mixer,
                        "crop": crop,
                        "picture": picture,
                        "row": row,
                        "y": crop[1] + y,
                        "pitch": pitch,
                        "first": crop[1] + first,
                        "bounds": bounds,
                    }
                )
        elif len(set(expected)) == 1:
            found = sorted(
                [row for row in rows if _normalize(row["text"]) == expected[0]],
                key=lambda row: row["box"][1],
            )
            if len(found) == 10:
                ys = [(row["box"][1] + row["box"][3]) / 2 for row in found]
                pitches = sorted(b - a for a, b in zip(ys, ys[1:]))
                pitch = pitches[len(pitches) // 2]
                if 14 <= pitch <= 90 and all(
                    abs(y - (ys[0] + n * pitch)) < 4 for n, y in enumerate(ys)
                ):
                    candidates.append(
                        {
                            "window": mixer,
                            "crop": crop,
                            "picture": picture,
                            "row": found[slot],
                            "y": crop[1] + ys[slot],
                            "pitch": pitch,
                            "first": crop[1] + ys[0],
                            "bounds": bounds,
                        }
                    )
    if len(candidates) != 1:
        raise RuntimeError(
            "Cannot uniquely resolve all effect-slot geometry; enlarge the Mixer inspector"
        )
    return candidates[0]


def _target_snapshot(conn, target):
    return call(
        conn,
        "snapshot",
        dict(target, parameter_limit=FULL_STATE_PARAMETER_LIMIT, include_names=True),
    )


def _track(conn, index):
    return call(conn, "trackSlots", {"index": index})["slots"]


def _assert_target(conn, target, before=None):
    current = call(conn, "assertTarget", target)
    if before is not None and identity(current) != identity(before):
        raise RuntimeError("Target plugin changed before the native operation")
    return current


def _slot_menu(windows, conn, target, before=None):
    slots = _track(conn, target["index"])
    layout = _slot_layout(windows, slots, target["slot_index"])
    row = layout["row"]["box"]
    # The arrow immediately before the independently recognized slot name opens
    # its menu. Verify menu text before any destructive/replace action.
    point = (round(layout["crop"][0] + row[0] - 11), round(layout["y"]))
    _assert_target(conn, target, before)
    windows._check_point(layout["window"], point)
    windows.click(point)
    windows.wait_window(class_name="TQuickPopupMenuWindow", timeout=3)
    return layout


def _choose(windows, labels):
    error = None
    for label in labels:
        try:
            windows.choose_menu(label, timeout=0.45)
            return
        except RuntimeError as exc:
            error = exc
    raise RuntimeError(
        "Expected FL plugin menu item was not found: " + ", ".join(labels)
    ) from error


def _wrapper_menu_button(windows, channel_name):
    def caption(text):
        return "".join(char for char in text if unicodedata.category(char) != "Co").strip()

    forms = set(
        windows.children(class_name="TPluginForm") + windows.windows(class_name="TPluginForm")
    )
    matching = [
        form
        for form in forms
        if (
            caption(windows.text(form)) == channel_name
            or (
                caption(windows.text(form)).startswith(channel_name + " (")
                and caption(windows.text(form)).endswith(")")
            )
        )
    ]
    if len(matching) != 1:
        raise RuntimeError("Cannot identify exactly one wrapper for the selected instrument")
    form = matching[0]
    title = caption(windows.text(form))
    captions = [
        hwnd
        for hwnd in windows.children(parent=form, class_name="TNewCaption")
        if caption(windows.text(hwnd)) == title
    ]
    if len(captions) != 1:
        raise RuntimeError("Instrument wrapper has no unique native caption")
    bounds = windows.rect(form)
    bar = windows.rect(captions[0])
    height = bar[3] - bar[1]
    if not (
        16 <= height <= 64
        and bar[2] - bar[0] >= 120
        and bounds[0] <= bar[0] < bar[2] <= bounds[2]
        and bounds[1] <= bar[1] < bar[3] <= bounds[3]
    ):
        raise RuntimeError("Unexpected native instrument wrapper caption geometry")
    # Observed FL26 TNewCaption: its first button is the menu triangle, centred
    # 10px from the left in a 23px title bar. Scale from the native bar height.
    point = (round(bar[0] + height * 0.45), round((bar[1] + bar[3]) / 2))
    return form, captions[0], bounds, bar, point


def _editor_menu(windows, conn, target, before=None):
    before = before or _target_snapshot(conn, target)
    call(conn, "openEditor", target)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        state = call(conn, "editorState", {})
        if (
            state.get("focused_generator") is True
            and state.get("focused_plugin_name") == before["plugin_name"]
            and state.get("mapped_global_index") == target["index"]
            and state.get("channel_global") == target["index"]
        ):
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("The exact plugin editor could not be focused to open its menu")
    form, caption, bounds, bar, point = _wrapper_menu_button(windows, before["channel_name"])
    _assert_target(conn, target, before)
    call(conn, "assertEditor", target)
    if windows.rect(form) != bounds or windows.rect(caption) != bar:
        raise RuntimeError("Instrument wrapper geometry changed before its menu opened")
    windows._check_point(caption, point)
    windows.click(point)
    windows.wait_window(class_name="TQuickPopupMenuWindow", timeout=3)


def _plugin_menu_row(windows, predicate, action, timeout=3):
    from PIL import ImageGrab

    deadline = time.monotonic() + timeout
    observed = []
    while time.monotonic() < deadline:
        windows.require_foreground()
        matches = []
        for menu in windows.windows(class_name="TQuickPopupMenuWindow"):
            bounds = windows.rect(menu)
            rows = asyncio.run(_ocr_lines(ImageGrab.grab(bbox=bounds, all_screens=True)))
            labels = {_normalize(row["text"]) for row in rows}
            observed = sorted(labels)
            if not {"replace", "browse presets"}.issubset(labels):
                continue
            for row in rows:
                if predicate(_normalize(row["text"])):
                    box = row["box"]
                    matches.append(
                        (
                            menu,
                            (
                                round(bounds[0] + (box[0] + box[2]) / 2),
                                round(bounds[1] + (box[1] + box[3]) / 2),
                            ),
                        )
                    )
        if len(matches) == 1:
            windows._check_point(*matches[0])
            windows.click(matches[0][1])
            return
        if len(matches) > 1:
            raise RuntimeError("Ambiguous plugin menu action: " + action)
        time.sleep(0.06)
    raise RuntimeError("Cannot read plugin " + action + "; observed: " + repr(observed))


def _choose_save_preset(windows):
    # Actual FL 26 captures lose the trailing 'as...' in Windows OCR. Match
    # only this exact observed row within a confirmed plugin menu, then validate
    # the resulting SaveAs dialog IDs before writing any destination.
    _plugin_menu_row(
        windows,
        lambda text: (
            text in ("save preset", "save preset as", "save preset as...", "save presets as...")
        ),
        "Save preset as",
    )


def _choose_delete(windows):
    # FL 26 has a top-level Delete... rather than Select > None. The exact
    # captured OCR punctuation was Delete.„; accept punctuation after Delete,
    # never another command such as Delete file.
    _plugin_menu_row(windows, lambda text: re.fullmatch(r"delete[^\w]*", text), "Delete")


def _save_dialog(windows, path):
    dialog = windows.wait_window(class_name="#32770", timeout=5)
    get_id = windows.user.GetDlgCtrlID
    get_id.argtypes = [ctypes.c_void_p]
    get_id.restype = ctypes.c_int
    # Observed on this host's localized Windows Common Item SaveAs dialog:
    # filename Edit=1001; IDOK=1. Other classes can also have ID1001.
    edits = [
        hwnd for hwnd in windows.children(parent=dialog, class_name="Edit") if get_id(hwnd) == 1001
    ]
    buttons = [
        hwnd for hwnd in windows.children(parent=dialog, class_name="Button") if get_id(hwnd) == 1
    ]
    if len(edits) != 1 or len(buttons) != 1:
        raise RuntimeError("Preset Save As has no unique filename Edit1001 and Save ID1")
    expected = str(path)
    windows.set_text(edits[0], expected)
    if windows.text(edits[0]) != expected:
        raise RuntimeError("Preset Save As filename readback did not match the exact destination")
    left, top, right, bottom = windows.rect(buttons[0])
    windows.click(((left + right) / 2, (top + bottom) / 2))


def save_preset(conn, target, path, overwrite=False, timeout=20):
    destination = Path(path)
    if not destination.is_absolute() or destination.suffix.casefold() != ".fst":
        raise ValueError("Preset destination must be an absolute .fst path")
    if destination.exists() and not overwrite:
        raise ValueError("Preset already exists; use overwrite=True explicitly")
    if not destination.parent.is_dir():
        raise ValueError("Preset destination directory does not exist")
    temporary = destination.parent / (".mcp-" + uuid.uuid4().hex + ".fst")
    before = _target_snapshot(conn, target)
    with FLWindows() as windows:
        call(conn, "prepareTarget", target)
        if target["kind"] == "effect":
            _slot_menu(windows, conn, target, before)
        else:
            _editor_menu(windows, conn, target, before)
        _choose_save_preset(windows)
        _save_dialog(windows, temporary)
    file_identity = wait_fst(temporary, timeout)
    after = _target_snapshot(conn, target)
    if identity(before) != identity(after):
        raise RuntimeError("Source plugin changed while its preset was being saved")
    if overwrite:
        temporary.replace(destination)
    else:
        # Windows rename fails if the destination was concurrently created.
        temporary.rename(destination)
    manifest = {
        "schema": 1,
        **file_identity,
        "plugin_name": before["plugin_name"],
        "display_name": before.get("display_name")
        or before.get("channel_name")
        or before["plugin_name"],
        "parameter_values": before.get("parameter_values"),
        "parameter_names": before.get("parameter_names"),
        "named_parameter_fingerprint": before.get("named_parameter_fingerprint"),
        "named_parameter_count": before.get("named_parameter_count"),
        "parameters_complete": before.get("parameters_complete", False),
        "parameter_count": before.get("parameter_count"),
        "source_kind": target["kind"],
        "verification_scope": "named_published_parameters",
    }
    metadata_path = destination.with_suffix(".fst.mcp.json")
    metadata_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "success": True,
        "status": "saved",
        "path": str(destination),
        **file_identity,
        "plugin_name": before["plugin_name"],
        "manifest": str(metadata_path),
        "verification_scope": "FST_chunks_and_published_parameters",
        "hidden_vst_state_independently_verified": False,
        "unnamed_state_verified": False,
    }


def _observe_preset(result, operation_id):
    if result.get("operation_id") not in (None, operation_id):
        raise RuntimeError("Preset status referred to a different operation")
    result["operation_id"] = operation_id
    if result.get("status") == "cancellation_requested":
        # A request acknowledges intent; observation still owns completion.
        result.update(status="loading", cancellation_requested=True)
    if "status" not in result:
        raise RuntimeError(result.get("error", "No preset operation status was returned"))
    preset_operations.update(result)
    return result


def _poll(conn, operation_id, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cached = preset_operations.get(operation_id) or {}
        if preset_operations.cancelled(operation_id) and not cached.get("cancellation_sent"):
            result = _observe_preset(
                conn.send_command("pluginManagement.cancel", {"operation_id": operation_id}),
                operation_id,
            )
            preset_operations.update(
                dict(result, cancellation_requested=True, cancellation_sent=True)
            )
            if result["status"] != "loading":
                return result
        result = _observe_preset(
            conn.send_command("pluginManagement.status", {"operation_id": operation_id}),
            operation_id,
        )
        if result["status"] != "loading" or result.get("stage") == "awaiting_drag":
            return result
        time.sleep(0.06)
    result = {
        "success": False,
        "status": "unknown",
        "operation_id": operation_id,
        "error": "Preset operation exceeded its deadline; inspect FL before retrying",
    }
    preset_operations.update(result)
    return result


def _drag_to_target(windows, conn, target, preset_name, operation_id):
    from PIL import ImageGrab
    from pynput.mouse import Button

    browsers = windows.children(class_name="TDataBrowserTree")
    if len(browsers) != 1:
        raise RuntimeError("Keep one expanded Browser visible while loading a preset")
    browser = browsers[0]
    bounds = windows.rect(browser)
    rows = asyncio.run(_ocr_lines(ImageGrab.grab(bbox=bounds, all_screens=True)))
    source_rows = [
        row
        for row in rows
        if _normalize(row["text"]) in (_normalize(preset_name), _normalize(preset_name + ".fst"))
    ]
    if len(source_rows) != 1:
        raise RuntimeError("The staged preset name is not uniquely visible in the Browser")
    box = source_rows[0]["box"]
    source = (round(bounds[0] + (box[0] + box[2]) / 2), round(bounds[1] + (box[1] + box[3]) / 2))
    if target["kind"] == "effect":
        layout = _slot_layout(windows, _track(conn, target["index"]), target["slot_index"])
        destination_window = layout["window"]
        destination_bounds = layout["bounds"]
        box = layout["row"]["box"]
        destination = (round(layout["crop"][0] + (box[0] + box[2]) / 2), round(layout["y"]))
    else:
        racks = windows.children(class_name="TStepSeqForm")
        if len(racks) != 1:
            raise RuntimeError("Keep the Channel Rack visible while replacing an instrument")
        name = _target_snapshot(conn, target)["channel_name"]
        destination_window = racks[0]
        try:
            destination = windows.find_text(destination_window, name)
        except RuntimeError:
            # An open editor can cover the lower rows when ALL shows more
            # channels. Bring this exact Rack forward through its owned caption,
            # then repeat the same full-name OCR and point ownership guards.
            captions = [
                hwnd
                for hwnd in windows.children(parent=destination_window, class_name="TNewCaption")
                if windows.text(hwnd) == "Channel rack"
            ]
            if len(captions) != 1:
                raise RuntimeError("Cannot identify the target Channel Rack caption") from None
            windows.click_text(captions[0], "Channel rack")
            destination = windows.find_text(destination_window, name)
        destination_bounds = windows.rect(destination_window)

    def check_geometry():
        windows.require_foreground()
        if (
            windows.rect(browser) != bounds
            or windows.rect(destination_window) != destination_bounds
        ):
            raise RuntimeError("FL window geometry changed before preset drop")
        windows._check_point(browser, source)
        windows._check_point(destination_window, destination)

    check_geometry()
    if preset_operations.cancelled(operation_id):
        raise RuntimeError("Cancelled before preset activation")
    call(conn, "confirm", {"operation_id": operation_id})
    preset_operations.update(
        {"operation_id": operation_id, "activation_issued": True, "stage": "awaiting_drop"}
    )
    if preset_operations.cancelled(operation_id):
        raise RuntimeError("Cancellation requested after confirming preset activation")
    windows.mouse.position = source
    windows.mouse.press(Button.left)
    try:
        for step in range(1, 26):
            windows.require_foreground()
            windows.mouse.position = (
                round(source[0] + (destination[0] - source[0]) * step / 25),
                round(source[1] + (destination[1] - source[1]) * step / 25),
            )
            time.sleep(0.02)
        check_geometry()
    except Exception:
        windows.mouse.position = source
        raise
    finally:
        windows.mouse.release(Button.left)
    # This is an acknowledgement of completed native input, not proof FL applied
    # the FST. The controller separately requires a before/after application witness.
    call(conn, "acknowledgeDrop", {"operation_id": operation_id})
    preset_operations.update(
        {"operation_id": operation_id, "drop_acknowledged": True, "stage": "verify"}
    )


def _refresh_browser(windows, conn, timeout=3):
    """Reread structure only while FL explicitly confirms Browser keyboard focus."""
    call(conn, "prepareBrowser", {})
    deadline = time.monotonic() + timeout

    def focused(state):
        return (
            state.get("focused") is True
            and not state.get("popup_open", True)
            and state.get("form_id") == state.get("browser_form_id") == 4
        )

    while time.monotonic() < deadline:
        state = call(conn, "getBrowserState", {})
        if focused(state):
            windows.require_foreground()
            # Ctrl+R is context dependent (Playlist uses it for export). Recheck
            # FL's actual focused form immediately before sending the shortcut.
            if not focused(call(conn, "getBrowserState", {})):
                raise RuntimeError("Browser focus changed; Ctrl+R was not sent")
            windows.press("ctrl+r")
            if not focused(call(conn, "getBrowserState", {})):
                raise RuntimeError("Browser focus changed after rereading its structure")
            # Reread is asynchronous: even a nonempty caption here may belong to
            # the old tree and disappear later. Always seed the visible root.
            # Reread structure clears FL26's selected node (caption='', type=-1).
            # The exact visible root row is confirmed by OCR and then by FL's
            # caption readback; no arrow navigation starts from a null selection.
            tree_deadline = time.monotonic() + timeout
            while time.monotonic() < tree_deadline:
                windows.require_foreground()
                trees = windows.children(class_name="TDataBrowserTree")
                if len(trees) == 1:
                    break
                if len(trees) > 1:
                    raise RuntimeError("Ambiguous visible Browser trees after refreshing structure")
                # FL26 briefly hides/rebuilds the tree while Ctrl+R rereads it.
                time.sleep(0.05)
            else:
                raise RuntimeError("Keep one Browser tree visible after refreshing its structure")
            if not focused(call(conn, "getBrowserState", {})):
                raise RuntimeError("Browser focus changed before selecting its visible root")
            windows.click_text(trees[0], "Current project")
            selection_deadline = time.monotonic() + timeout
            stable = 0
            while time.monotonic() < selection_deadline:
                selected = call(conn, "getBrowserState", {})
                if not focused(selected):
                    raise RuntimeError("Browser focus changed while confirming its root selection")
                if selected.get("node_caption", "").casefold() == "current project":
                    stable += 1
                    if stable >= 2:
                        return
                else:
                    stable = 0
                time.sleep(0.05)
            raise RuntimeError("Browser root selection was not confirmed after refresh")
        time.sleep(0.05)
    raise RuntimeError("Browser focus was not confirmed; Ctrl+R was not sent")


def _staging_leaf_name(source, manifest):
    label = (manifest or {}).get("display_name")
    if label is None or label == "":
        label = source.stem
    invalid = not isinstance(label, str) or not label or label in (".", "..")
    if not invalid:
        invalid = (
            label.endswith((".", " "))
            or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in label)
            or len((label + ".fst").encode("utf-16-le")) // 2 > 255
        )
    reserved = {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
    reserved.update(prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³")
    if invalid or label.split(".", 1)[0].rstrip(" ").upper() in reserved:
        raise ValueError(
            "Preset display name must be a complete legal Windows basename; "
            "use a valid label without reserved names, separators or trailing dots/spaces"
        )
    return label + ".fst"


def load_preset(
    conn, target, path, user_data, replace_existing=False, timeout=60, expected_loaded_plugin=None
):
    source = Path(path)
    if not source.is_absolute() or source.suffix.casefold() != ".fst" or not source.is_file():
        raise ValueError("Preset source must be an existing absolute .fst path")
    info = validate_fst(source)
    metadata = source.with_suffix(".fst.mcp.json")
    manifest = None
    if metadata.is_file():
        candidate = json.loads(metadata.read_text(encoding="utf-8"))
        if (
            isinstance(candidate, dict)
            and candidate.get("schema") == 1
            and (candidate.get("sha256") == info["sha256"])
        ):
            manifest = candidate
    leaf_name = _staging_leaf_name(source, manifest)
    before = _target_snapshot(conn, target)
    if before["occupied"] and not replace_existing:
        raise ValueError("Replacing an occupied plugin requires replace_existing=True")
    folder = (
        Path(user_data)
        / "Presets"
        / "Plugin database"
        / "MCP Presets"
        / ("Effects" if target["kind"] == "effect" else "Generators")
    )
    folder.mkdir(parents=True, exist_ok=True)
    # Keep the leaf's real display name: FL can adopt an FST leaf as the slot or
    # channel label. Isolate each load with an exclusively created private folder,
    # rather than leaking a unique internal token into the visible plugin name.
    words = (
        "amber",
        "birch",
        "cedar",
        "delta",
        "echo",
        "forest",
        "garden",
        "harbor",
        "jade",
        "maple",
        "pearl",
        "river",
        "stone",
        "tiger",
        "water",
        "zenith",
    )
    for _ in range(32):
        token = " ".join(words[value % len(words)] for value in uuid.uuid4().bytes[:3])
        staged_directory = folder / ("MCP " + token)
        try:
            staged_directory.mkdir()
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError("Cannot allocate a unique readable Browser staging directory")
    staged = staged_directory / leaf_name
    created_leaf = False
    try:
        with staged.open("xb") as output:
            created_leaf = True
            output.write(source.read_bytes())
    except Exception:
        if created_leaf:
            staged.unlink(missing_ok=True)
        try:
            staged_directory.rmdir()
        except OSError:
            pass
        raise
    args = dict(
        target,
        relative_id=staged.relative_to(folder.parents[1]).as_posix(),
        replace_existing=replace_existing,
        timeout=timeout,
        expected_loaded_plugin=(manifest or {}).get("plugin_name") or expected_loaded_plugin,
        expected_names=(manifest or {}).get("parameter_names")
        if (manifest or {}).get("parameters_complete")
        else None,
        expected_values=(manifest or {}).get("parameter_values")
        if (manifest or {}).get("parameters_complete")
        else None,
    )
    operation_id = None
    result = None
    try:
        with FLWindows() as windows:
            _refresh_browser(windows, conn)
            result = call(conn, "beginPreset", args)
            operation_id = result["operation_id"]
            preset_operations.begin(
                operation_id,
                success=True,
                kind=target["kind"],
                source_path=str(source),
                staged_preset=str(staged),
                staged_directory=str(staged_directory),
                runner_active=True,
            )
            preset_operations.update(result)
            deadline = time.monotonic() + timeout
            result = _poll(conn, operation_id, max(0, deadline - time.monotonic()))
            if result.get("status") != "loading":
                return result
            _drag_to_target(windows, conn, target, staged.stem, operation_id)
            result = _poll(conn, operation_id, max(0, deadline - time.monotonic()))
            while result.get("status") in ("loading", "cancellation_requested"):
                result = _poll(conn, operation_id, max(0, deadline - time.monotonic()))
            return result
    except Exception as exc:
        if operation_id is None:
            raise
        try:
            result = conn.send_command("pluginManagement.cancel", {"operation_id": operation_id})
        except Exception:
            result = {"success": False, "status": "unknown", "activation_issued": True}
        result.update(
            success=False, error=str(exc), operation_id=operation_id, inspect_before_retrying=True
        )
        return result
    finally:
        if operation_id is not None:
            if result is None:
                result = {
                    "success": False,
                    "operation_id": operation_id,
                    "status": "unknown",
                    "error": "Preset runner exited without a verified terminal result",
                }
            result.setdefault("operation_id", operation_id)
            if result.get("status") in (None, "loading", "cancellation_requested"):
                result.update(
                    success=False,
                    status="unknown",
                    inspect_before_retrying=True,
                    activation_may_have_occurred=bool(result.get("activation_issued")),
                    error=result.get("error") or "Preset runner stopped; inspect FL",
                )
            result["runner_active"] = False
            preset_operations.update(result)
        # A late FL activation may still need this file. Unknown/issued failures
        # keep their unique staged artifact until the user inspects the destination.
        safe = operation_id is None or (
            result is not None
            and (
                result.get("status") == "loaded"
                or (
                    result.get("status") in ("failed", "cancelled")
                    and result.get("activation_issued") is False
                )
            )
        )
        if safe:
            staged.unlink(missing_ok=True)
            try:
                staged_directory.rmdir()
            except OSError as exc:
                if result is not None:
                    result["staging_cleanup_error"] = str(exc)
                    result["staged_directory"] = str(staged_directory)
            if result is not None:
                result["staged_preset"] = None
                if not staged_directory.exists():
                    result["staged_directory"] = None
        elif result is not None:
            result["staged_preset"] = str(staged)
            result["staged_directory"] = str(staged_directory)
        if operation_id is not None and result is not None:
            preset_operations.update(result)


def _confirm_plugin_delete(windows, kind):
    from PIL import ImageGrab

    prompts = {
        "generator": "the selected channel is about to be deleted.",
        "effect": "the selected plugin is about to be deleted.",
    }
    if kind not in prompts:
        raise ValueError("Unknown plugin deletion kind")
    expected_prompt = prompts[kind]

    dialogs = windows.windows(class_name="TMsgForm", title="Confirm")
    if not dialogs:
        return False
    if len(dialogs) != 1:
        raise RuntimeError("Ambiguous plugin deletion confirmation")
    dialog = dialogs[0]
    bounds = windows.rect(dialog)
    deadline = time.monotonic() + 1.5
    while True:
        rows = asyncio.run(_ocr_lines(ImageGrab.grab(bbox=bounds, all_screens=True)))
        if any(
            _normalize(row["text"]) == expected_prompt for row in rows
        ):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Unexpected confirmation while deleting the target plugin")
        windows.require_foreground()
        if windows.rect(dialog) != bounds:
            raise RuntimeError("Plugin deletion confirmation moved before it was verified")
        time.sleep(0.05)
    buttons = [row for row in rows if _normalize(row["text"]) in ("ok", "0k")]
    if len(buttons) != 1:
        raise RuntimeError("Cannot uniquely identify the plugin deletion Ok button")
    box = buttons[0]["box"]
    point = (round(bounds[0] + (box[0] + box[2]) / 2), round(bounds[1] + (box[1] + box[3]) / 2))
    windows._check_point(dialog, point)
    windows.click(point)
    return True


def delete_plugin(conn, target, timeout=8):
    before = _target_snapshot(conn, target)
    if not before["occupied"]:
        raise ValueError("There is no plugin to delete")
    if target["kind"] == "effect":
        previous = _track(conn, target["index"])
    else:
        previous = call(conn, "inventory", {})["instruments"]
    with FLWindows() as windows:
        call(conn, "prepareTarget", target)
        if target["kind"] == "effect":
            _slot_menu(windows, conn, target, before)
            _choose_delete(windows)
        else:
            _editor_menu(windows, conn, target, before)
            _choose_delete(windows)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _confirm_plugin_delete(windows, target["kind"])
            if target["kind"] == "effect":
                after = _track(conn, target["index"])
                if not after[target["slot_index"]]["occupied"]:
                    if any(
                        identity(a) != identity(b)
                        for n, (a, b) in enumerate(zip(previous, after))
                        if n != target["slot_index"]
                    ):
                        raise RuntimeError("A neighboring effect changed during deletion")
                    return {"success": True, "status": "deleted", **target}
            else:
                after = call(conn, "inventory", {})["instruments"]
                if len(after) == len(previous) - 1:
                    expected = previous[: target["index"]] + previous[target["index"] + 1 :]
                    # Global indices shift after deletion. Compare the surviving
                    # observable channel properties; a matching plugin name alone
                    # does not prove that the requested instance was removed.
                    fields = (
                        "occupied",
                        "plugin_name",
                        "display_name",
                        "channel_name",
                        "mixer_track",
                        "enabled",
                    )
                    if any(
                        any(a.get(key) != b.get(key) for key in fields)
                        for a, b in zip(after, expected)
                    ):
                        raise RuntimeError("Unexpected Channel Rack changes during deletion")
                    return {"success": True, "status": "deleted", **target}
            time.sleep(0.08)
    raise RuntimeError("Plugin deletion was not verified; inspect any confirmation dialog in FL")


def move_effect_within_track(conn, track, source_slot, destination_slot, expected_plugin=None):
    target = {
        "kind": "effect",
        "index": track,
        "slot_index": source_slot,
        "expected_plugin": expected_plugin,
    }
    before = _track(conn, track)
    if not before[source_slot]["occupied"]:
        raise ValueError("Source effect slot is empty")
    if expected_plugin is not None and before[source_slot]["plugin_name"] != expected_plugin:
        raise ValueError("Source plugin identity changed")
    expected = list(before)
    with FLWindows() as windows:
        for slot in range(
            source_slot, destination_slot, 1 if destination_slot > source_slot else -1
        ):
            next_slot = slot + (1 if destination_slot > source_slot else -1)
            current_target = dict(target, slot_index=slot)
            call(conn, "prepareTarget", current_target)
            current = _target_snapshot(conn, current_target)
            _slot_menu(windows, conn, current_target, current)
            _choose(
                windows, ("Move down", "Move Down") if next_slot > slot else ("Move up", "Move Up")
            )
            expected[slot], expected[next_slot] = expected[next_slot], expected[slot]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                actual = _track(conn, track)
                if all(identity(a) == identity(b) for a, b in zip(expected, actual)):
                    break
                time.sleep(0.06)
            else:
                raise RuntimeError("Effect reorder did not match the expected native slot swap")
    return {
        "success": True,
        "status": "moved",
        "source_track": track,
        "source_slot": source_slot,
        "destination_track": track,
        "destination_slot": destination_slot,
        "automation_links_preserved": True,
        "method": "native_slot_reorder",
    }


def read_effect_enabled(conn, target):
    return _effect_enable_state(conn, target, None)


def set_effect_enabled(conn, target, enabled):
    return _effect_enable_state(conn, target, enabled)


def _effect_enable_state(conn, target, enabled):
    from PIL import ImageGrab

    call(conn, "prepareTarget", target)
    with FLWindows() as windows:
        slots = _track(conn, target["index"])
        layout = _slot_layout(windows, slots, target["slot_index"])
        bounds = layout["bounds"]
        crop = (max(bounds[0], bounds[2] - 35), layout["crop"][1], bounds[2], layout["crop"][3])

        def state():
            image = ImageGrab.grab(bbox=crop, all_screens=True).convert("RGB")
            markers = []
            for n in range(10):
                y = round(layout["first"] + n * layout["pitch"] - crop[1])
                points = [
                    (x, yy)
                    for yy in range(max(0, y - 6), min(image.height, y + 7))
                    for x in range(image.width)
                    if (lambda c: c[1] > 100 and c[1] > c[0] * 1.25 and c[1] > c[2] * 1.25)(
                        image.getpixel((x, yy))
                    )
                ]
                if len(points) >= 3:
                    markers.append((n, sum(x for x, unused in points) / len(points)))
            if len(markers) < 2:
                raise RuntimeError("Cannot independently read the effect-enable LEDs in this theme")
            columns = sorted(x for unused, x in markers)
            column = columns[len(columns) // 2]
            if any(abs(x - column) > 3 for unused, x in markers):
                raise RuntimeError("Effect-enable LED columns are ambiguous")
            y = round(layout["y"] - crop[1])
            count = sum(
                1
                for yy in range(max(0, y - 5), min(image.height, y + 6))
                for x in range(max(0, round(column) - 5), min(image.width, round(column) + 6))
                if (lambda c: c[1] > 100 and c[1] > c[0] * 1.25 and c[1] > c[2] * 1.25)(
                    image.getpixel((x, yy))
                )
            )
            return count >= 3, (round(crop[0] + column), round(layout["y"]))

        old, point = state()
        if enabled is not None and old != enabled:
            _assert_target(conn, target)
            windows.click(point)
        actual, unused = state()
        deadline = time.monotonic() + 2
        while enabled is not None and actual != enabled and time.monotonic() < deadline:
            windows.require_foreground()
            time.sleep(0.06)
            actual, unused = state()
        if enabled is not None and actual != enabled:
            raise RuntimeError("Effect-enable LED did not reach the requested state")
        return {"success": True, "enabled": actual, "enabled_source": "mixer_enable_led", **target}
