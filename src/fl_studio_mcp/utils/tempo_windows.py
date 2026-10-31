"""Read FL's displayed tempo from an owned toolbar word and its native hover hint."""

from __future__ import annotations

import json
import math
import re
import time

from fl_studio_mcp.utils.arrangement_windows import _picture_records, _scan
from fl_studio_mcp.utils.windows_ui import FLWindows

_TOOLBAR_CLASSES = {"tquickedittoolbar", "tquickedittoolbarpanel"}
_RATIOS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
_NUMBER = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,3})?")
_TEMPO_WORD = re.compile(r"\btempo\b", re.IGNORECASE)
_HOVER_DELAY = 0.12
_HINT_DELAY = 0.06


def _command(connection, action):
    result = connection.send_command(action, {}, timeout=5.0)
    if not result.get("success") or result.get("error"):
        raise RuntimeError(result.get("error", "FL rejected " + action))
    return result


def _quarter_bpm(timing):
    value = timing.get("quarter_bpm", timing.get("bpm"))
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise RuntimeError("FL did not return a finite positive quarter-note BPM")
    return float(value)


def _ratios(timing):
    denominator = timing.get("denominator")
    if denominator is None:
        return _RATIOS
    if type(denominator) is not int or denominator / 4 not in _RATIOS:
        raise RuntimeError("FL returned an unsupported time-signature denominator")
    return (denominator / 4,)


def _points(rect):
    left, top, right, bottom = rect
    return (
        (left, top),
        (right - 1, top),
        (left, bottom - 1),
        (right - 1, bottom - 1),
        ((left + right) / 2, (top + bottom) / 2),
    )


def _guard(window, hwnd, bounds, root_bounds, rect=None):
    window.require_foreground()
    if window.rect(window.root) != root_bounds or window.rect(hwnd) != bounds:
        raise RuntimeError("FL toolbar geometry changed while reading tempo")
    if window.class_name(hwnd).casefold() not in _TOOLBAR_CLASSES:
        raise RuntimeError("The tempo toolbar control changed")
    for point in _points(rect or bounds):
        window._check_point(hwnd, point)


def _candidate_words(records, bounds, quarter_bpm, ratios):
    for record in records:
        # A line can contain Memory, CPU and Tempo together. Only OCR word boxes
        # identify the value that will actually receive the hover.
        for word in record.get("words", ()):
            text = str(word.get("text", "")).strip()
            if not _NUMBER.fullmatch(text):
                continue
            value = float(text)
            if not 10 <= value <= 522:
                continue
            rect = word.get("rect", ())
            if (
                len(rect) != 4
                or any(not math.isfinite(part) for part in rect)
                or not bounds[0] <= rect[0] < rect[2] <= bounds[2]
                or not bounds[1] <= rect[1] < rect[3] <= bounds[3]
            ):
                continue
            matching = [
                ratio
                for ratio in ratios
                if math.isclose(value, quarter_bpm * ratio, rel_tol=0, abs_tol=0.001)
            ]
            if len(matching) == 1:
                yield value, matching[0], tuple(round(part) for part in rect)


def _narrow_regions(bounds):
    """Overlap small OCR strips so toolbar graphics do not hide nearby digits."""
    left, top, right, bottom = bounds

    def starts(low, high):
        last = max(low, high - 64)
        return sorted(set([*range(low, last + 1, 32), last]))

    regions = [
        (x, y, min(x + 64, right), min(y + 64, bottom))
        for y in starts(top, bottom)
        for x in starts(left, right)
    ]
    if len(regions) > 64:
        raise RuntimeError("The FL toolbar exceeds the bounded narrow OCR budget")
    return regions


def _narrow_records(window, hwnd, bounds, pre_scale=1):
    from PIL import ImageGrab

    window.require_foreground()
    picture = ImageGrab.grab(bbox=bounds, all_screens=True)
    records = []
    for region in _narrow_regions(bounds):
        crop = tuple(region[index] - bounds[index % 2] for index in range(4))
        records.extend(_picture_records(picture.crop(crop), region, pre_scale=pre_scale))
    return records


def _same_word_box(first, second):
    width = max(0, min(first[2], second[2]) - max(first[0], second[0]))
    height = max(0, min(first[3], second[3]) - max(first[1], second[1]))
    area = min(
        (first[2] - first[0]) * (first[3] - first[1]),
        (second[2] - second[0]) * (second[3] - second[1]),
    )
    return area > 0 and width * height >= area * 0.6


def _hint_summary(value):
    # Diagnostics contain only bounded toolbar values/geometry and short native
    # hints, never screenshots, project filenames or unrestricted OCR text.
    return " ".join(value.split())[:96]


def _read_display_tempo(connection, window):
    original_pointer = tuple(window.mouse.position)
    try:
        window.require_foreground()
        root_bounds = window.rect(window.root)
        timing = _command(connection, "arrangement.getTiming")
        quarter_bpm = _quarter_bpm(timing)
        ratios = _ratios(timing)
        toolbars = [
            hwnd
            for hwnd in window.children(parent=window.root)
            if window.class_name(hwnd).casefold() in _TOOLBAR_CLASSES
        ]
        if not toolbars or len(toolbars) > 64:
            raise RuntimeError("No bounded FL main-toolbar controls are available")
        toolbar_set = set(toolbars)
        # The parent toolbar and its individual value controls can expose the
        # same pixels. Small native leaf controls also avoid whole-line OCR
        # dropping the tempo digits beside graphical transport buttons.
        toolbars = [
            hwnd for hwnd in toolbars if not toolbar_set.intersection(window.children(parent=hwnd))
        ]
        candidates = {}
        strip_count = 0
        for hwnd in toolbars:
            bounds = window.rect(hwnd)
            if (
                not root_bounds[0] <= bounds[0] < bounds[2] <= root_bounds[2]
                or not root_bounds[1] <= bounds[1] < bounds[3] <= root_bounds[3]
                or bounds[2] - bounds[0] > 4096
                or bounds[3] - bounds[1] > 256
            ):
                raise RuntimeError("Unexpected FL toolbar dimensions")
            _guard(window, hwnd, bounds, root_bounds)
            records = _scan(window, hwnd, region=bounds)
            _guard(window, hwnd, bounds, root_bounds)
            words = list(_candidate_words(records, bounds, quarter_bpm, ratios))
            if not words:
                # FL's small decimal glyphs can merge into letters at the first
                # OCR scale (for example 160J00). Re-read pixels, never coerce
                # those letters. _picture_records maps enlarged boxes back to
                # the same native leaf, and every pass counts toward the budget.
                for pre_scale in (1, 2):
                    strip_count += len(_narrow_regions(bounds))
                    if strip_count > 256:
                        raise RuntimeError(
                            "FL toolbar narrow OCR exceeded its total bounded budget"
                        )
                    records = _narrow_records(window, hwnd, bounds, pre_scale)
                    _guard(window, hwnd, bounds, root_bounds)
                    words = list(_candidate_words(records, bounds, quarter_bpm, ratios))
                    if words:
                        break
            for value, ratio, rect in words:
                _guard(window, hwnd, bounds, root_bounds, rect)
                item = (value, ratio, hwnd, bounds)
                overlaps = [
                    other
                    for other in candidates
                    if other[0] == hwnd and _same_word_box(rect, other[1])
                ]
                if any(candidates[other][:2] != item[:2] for other in overlaps):
                    raise RuntimeError("Conflicting OCR values occupy the same tempo word box")
                if not overlaps:
                    candidates[(hwnd, rect)] = item
        if not candidates or len(candidates) > 32:
            raise RuntimeError("No bounded numeric toolbar candidates match quarter-note BPM")
        verified = []
        summaries = []
        for (_, rect), (value, ratio, hwnd, bounds) in candidates.items():
            _guard(window, hwnd, bounds, root_bounds, rect)
            point = tuple(round((rect[axis] + rect[axis + 2]) / 2) for axis in (0, 1))
            window.mouse.position = point
            time.sleep(_HOVER_DELAY)
            _guard(window, hwnd, bounds, root_bounds, rect)
            if tuple(round(part) for part in window.mouse.position) != point:
                raise RuntimeError("Pointer moved away from the tempo candidate")
            first = str(_command(connection, "system.getHint").get("hint", ""))
            time.sleep(_HINT_DELAY)
            _guard(window, hwnd, bounds, root_bounds, rect)
            if tuple(round(part) for part in window.mouse.position) != point:
                raise RuntimeError("Pointer moved away from the tempo candidate")
            second = str(_command(connection, "system.getHint").get("hint", ""))
            if tuple(round(part) for part in window.mouse.position) != point:
                raise RuntimeError("Pointer moved away from the tempo candidate")
            stable = first == second
            tempo_words = [bool(_TEMPO_WORD.search(hint)) for hint in (first, second)]
            accepted = stable and all(tempo_words)
            summaries.append(
                {
                    "bpm": value,
                    "ratio": ratio,
                    "hwnd": hwnd,
                    "rect": rect,
                    "hints": [_hint_summary(first), _hint_summary(second)],
                    "hint_stable": stable,
                    "tempo_words": tempo_words,
                    "accepted": accepted,
                }
            )
            if accepted:
                verified.append((value, ratio, hwnd, bounds, rect))
        if len(verified) != 1:
            diagnostic = json.dumps(
                {
                    "quarter_bpm": quarter_bpm,
                    "candidate_count": len(candidates),
                    "confirmed_count": len(verified),
                    "candidates": summaries,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            raise RuntimeError(
                "Expected exactly one toolbar BPM word confirmed by a Tempo hint; "
                "diagnostic=" + diagnostic
            )
        final = _command(connection, "arrangement.getTiming")
        window.require_foreground()
        if (
            not math.isclose(_quarter_bpm(final), quarter_bpm, rel_tol=0, abs_tol=1e-8)
            or _ratios(final) != ratios
            or window.rect(window.root) != root_bounds
        ):
            raise RuntimeError("FL tempo or toolbar geometry changed during verification")
        value, ratio, hwnd, bounds, rect = verified[0]
        _guard(window, hwnd, bounds, root_bounds, rect)
        return {
            "bpm": value,
            "quarter_bpm": quarter_bpm,
            "tempo_unit": "displayed_bpm",
            "ui_to_quarter_ratio": ratio,
            "verification": "toolbar_ocr_word_and_stable_tempo_hover_hint",
        }
    finally:
        window.mouse.position = original_pointer


def read_display_tempo(connection, window=None) -> dict:
    """Read displayed BPM without editing values; restore the pointer on every exit."""
    with connection.transaction():
        if window is not None:
            return _read_display_tempo(connection, window)
        with FLWindows() as active:
            return _read_display_tempo(connection, active)
