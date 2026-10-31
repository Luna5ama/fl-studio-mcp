"""Bounded Windows Playlist workflows with MIDI selection-boundary readback.

The MIDI API does not enumerate ordinary clips. OCR results deliberately describe
only visible labels, and a mutation is confirmed with FL's selection tick range.
"""

from __future__ import annotations

import asyncio
import math
import re
import statistics
import time
from dataclasses import dataclass

from fl_studio_mcp.utils.windows_ui import FLWindows

MAX_SCAN_HEIGHT = 1100
RULER_STRIP_WIDTH = 256
RULER_STRIP_OVERLAP = 32
MAX_RULER_STRIPS = 16


def _normal(text):
    return "".join(text.replace("&", "").casefold().split())


def _command(connection, action, params=None):
    result = connection.send_command(action, params or {}, timeout=5.0)
    if not result.get("success"):
        raise RuntimeError(result.get("error", "FL Studio rejected " + action))
    return result


async def _recognize(picture):
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    engine = OcrEngine.try_create_from_language(Language("en-US"))
    if engine is None:
        raise RuntimeError("Windows English OCR language is required")
    scale = min(4, max(1, OcrEngine.max_image_dimension // max(picture.size)))
    picture = picture.convert("RGBA").resize((picture.width * scale, picture.height * scale))
    bitmap = SoftwareBitmap(
        BitmapPixelFormat.BGRA8, picture.width, picture.height, BitmapAlphaMode.IGNORE
    )
    writer = DataWriter()
    writer.write_bytes(picture.tobytes("raw", "BGRA"))
    bitmap.copy_from_buffer(writer.detach_buffer())
    result = await engine.recognize_async(bitmap)
    return [
        {
            "text": line.text,
            "words": [
                {
                    "text": word.text,
                    "rect": (
                        word.bounding_rect.x / scale,
                        word.bounding_rect.y / scale,
                        (word.bounding_rect.x + word.bounding_rect.width) / scale,
                        (word.bounding_rect.y + word.bounding_rect.height) / scale,
                    ),
                }
                for word in line.words
            ],
        }
        for line in result.lines
    ]


def _scan_bounds(bounds, region=None):
    if region is None:
        return bounds[0], bounds[1], bounds[2], min(bounds[3], bounds[1] + MAX_SCAN_HEIGHT)
    return (
        max(bounds[0], region[0]),
        max(bounds[1], region[1]),
        min(bounds[2], region[2]),
        min(bounds[3], region[3]),
    )


def _picture_records(picture, bounds, contrast=1, pre_scale=1):
    if contrast != 1:
        from PIL import ImageEnhance, ImageOps

        picture = ImageEnhance.Contrast(ImageOps.grayscale(picture)).enhance(contrast)
    if pre_scale != 1:
        from PIL import Image

        picture = picture.resize(
            (round(picture.width * pre_scale), round(picture.height * pre_scale)),
            Image.Resampling.BICUBIC,
        )
    records = asyncio.run(_recognize(picture))
    for record in records:
        for word in record["words"]:
            rect = word["rect"]
            word["rect"] = (
                rect[0] / pre_scale + bounds[0],
                rect[1] / pre_scale + bounds[1],
                rect[2] / pre_scale + bounds[0],
                rect[3] / pre_scale + bounds[1],
            )
        rects = [word["rect"] for word in record["words"]]
        record["rect"] = (
            min(r[0] for r in rects),
            min(r[1] for r in rects),
            max(r[2] for r in rects),
            max(r[3] for r in rects),
        )
    return records


def _merge_header_readings(records, readings, track_names, left, right, ruler_y):
    """Merge exact same-position readings; conflicting identities make that row unusable."""
    normalized = [_normal(name) for name in track_names]
    names = {name for name in normalized if normalized.count(name) == 1}

    def candidate(record):
        rect = record["rect"]
        return (
            _normal(record["text"]) in names
            and left <= rect[0]
            and rect[2] < right
            and rect[1] > ruler_y + 6
        )

    accepted = []
    blocked = []
    for record in [record for record in records if candidate(record)] + readings:
        if not candidate(record):
            continue
        rect = record["rect"]
        y = (rect[1] + rect[3]) / 2
        tolerance = max(4, (rect[3] - rect[1]) * 0.75)
        if any(abs(y - other) <= tolerance for other in blocked):
            continue
        matches = [
            item
            for item in accepted
            if abs(y - (item["rect"][1] + item["rect"][3]) / 2) <= tolerance
        ]
        if any(_normal(item["text"]) != _normal(record["text"]) for item in matches):
            accepted = [item for item in accepted if item not in matches]
            blocked.append(y)
        elif not matches:
            accepted.append(record)
    return [record for record in records if not candidate(record)] + accepted


def _enhance_headers(picture, bounds, records, tracks):
    """Read narrow strips and incomplete rows without inventing an index from neighbors."""
    grid = _calibrate(records, [], bounds, require_tracks=False)
    names = {_normal(track["name"]) for track in tracks}
    anchors = [
        record
        for record in records
        if _normal(record["text"]) in names
        and record["rect"][2] < grid.canvas_left
        and record["rect"][1] > grid.ruler_y + 6
    ]
    if not anchors:
        return records
    left = max(bounds[0], math.floor(min(record["rect"][0] for record in anchors) - 14))
    right = min(grid.canvas_left, math.ceil(max(record["rect"][2] for record in anchors) + 100))
    top = max(bounds[1], math.floor(grid.ruler_y + 8))
    readings = []

    def read(region, contrast=2, pre_scale=1):
        region = tuple(round(part) for part in region)
        local = (
            region[0] - bounds[0],
            region[1] - bounds[1],
            region[2] - bounds[0],
            region[3] - bounds[1],
        )
        return _picture_records(picture.crop(local), region, contrast, pre_scale)

    # Small strips allow 4x OCR even on tall displays. Adjacent strips overlap
    # so a text line crossing a strip edge can still be read in full.
    for y in range(top, bounds[3], 300):
        readings.extend(read((left, y, right, min(y + 320, bounds[3]))))
    pending = []
    for record in records + readings:
        rect = record["rect"]
        y = (rect[1] + rect[3]) / 2
        if (
            _normal(record["text"]) in names
            or not left <= rect[0] < rect[2] < right
            or rect[1] <= grid.ruler_y + 6
            or any(abs(y - existing) < 6 for existing in pending)
        ):
            continue
        pending.append(y)
        if len(pending) > 64:
            break
        height = max(12, rect[3] - rect[1])
        region = (
            left,
            max(top, math.floor(y - height)),
            right,
            min(bounds[3], math.ceil(y + height)),
        )
        for contrast, pre_scale in ((2, 1), (3, 1.5)):
            readings.extend(read(region, contrast, pre_scale))
    return _merge_header_readings(
        records, readings, [track["name"] for track in tracks], left, grid.canvas_left, grid.ruler_y
    )


def _ruler_strip(window, hwnd, bounds):
    """Locate a narrow ruler band without needing any readable ruler labels."""
    window.require_foreground()
    class_name = window.class_name(hwnd)
    if class_name != "TPlaylistForm" and not (
        class_name == "TEventEditForm" and window.text(hwnd).casefold().startswith("playlist -")
    ):
        raise RuntimeError("Ruler recovery requires the native Playlist window")
    parent = window.rect(hwnd)
    candidates = []
    for child in dict.fromkeys(window.children(parent=hwnd, class_name="TQuickScroller")):
        rect = window.rect(child)
        left, top, right, bottom = rect
        width, height = right - left, bottom - top
        if (
            height <= 0
            or width / height <= 5
            or width <= (parent[2] - parent[0]) / 2
            or not (parent[0] <= left < right <= parent[2])
            or not (parent[1] <= top < bottom <= min(parent[1] + 96, parent[3]))
        ):
            continue
        strip = (left, bottom, right, bottom + 24)
        if not (
            bounds[0] <= strip[0] < strip[2] <= bounds[2]
            and bounds[1] <= strip[1] < strip[3] <= bounds[3]
        ):
            continue
        candidates.append((child, rect, strip))
    if len(candidates) != 1:
        raise RuntimeError("Cannot identify one native Playlist ruler scroller for OCR recovery")
    child, rect, strip = candidates[0]
    center = ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
    window._check_point(hwnd, center)
    window._check_point(child, center)
    return parent, child, rect, strip


def _merge_ruler_readings(records, readings, strip):
    def numeric(word):
        return re.fullmatch(r"[1-9][0-9]{0,5}", word["text"]) is not None

    def in_band(word):
        left, top, right, bottom = word["rect"]
        return (
            strip[0] <= (left + right) / 2 < strip[2] and strip[1] <= (top + bottom) / 2 < strip[3]
        )

    def same_box(first, second):
        a, b = first["rect"], second["rect"]
        overlap = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
            0, min(a[3], b[3]) - max(a[1], b[1])
        )
        area = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
        return area > 0 and overlap / area >= 0.6

    accepted, blocked = [], []
    for record in readings:
        for word in record["words"]:
            if not numeric(word) or not in_band(word) or any(same_box(word, b) for b in blocked):
                continue
            matches = [other for other in accepted if same_box(word, other)]
            if any(other["text"] != word["text"] for other in matches):
                accepted = [other for other in accepted if other not in matches]
                blocked.extend([word, *matches])
            elif not matches:
                accepted.append(word)
    result = []
    for record in records:
        words = [word for word in record["words"] if not (numeric(word) and in_band(word))]
        if not words:
            continue
        rects = [word["rect"] for word in words]
        result.append(
            {
                **record,
                "text": " ".join(word["text"] for word in words),
                "words": words,
                "rect": (
                    min(rect[0] for rect in rects),
                    min(rect[1] for rect in rects),
                    max(rect[2] for rect in rects),
                    max(rect[3] for rect in rects),
                ),
            }
        )
    return result + [
        {"text": word["text"], "rect": word["rect"], "words": [word]} for word in accepted
    ]


def _recover_ruler(window, hwnd, picture, bounds, records):
    parent, child, scroller, strip = _ruler_strip(window, hwnd, bounds)
    step = RULER_STRIP_WIDTH - RULER_STRIP_OVERLAP
    segments = list(range(strip[0], strip[2], step))
    if len(segments) > MAX_RULER_STRIPS:
        raise RuntimeError("Playlist ruler exceeds the bounded OCR recovery budget")
    readings = []
    for left in segments:
        window.require_foreground()
        if window.rect(hwnd) != parent or window.rect(child) != scroller:
            raise RuntimeError("Playlist ruler geometry changed during OCR recovery")
        region = (left, strip[1], min(left + RULER_STRIP_WIDTH, strip[2]), strip[3])
        for point in (
            (region[0] + 1, region[1] + 1),
            (region[2] - 1, region[1] + 1),
            (region[0] + 1, region[3] - 1),
            (region[2] - 1, region[3] - 1),
        ):
            window._check_point(hwnd, point)
        local = (
            region[0] - bounds[0],
            region[1] - bounds[1],
            region[2] - bounds[0],
            region[3] - bounds[1],
        )
        for record in _picture_records(picture.crop(local), region, contrast=2, pre_scale=2):
            # Discard edge fragments; the overlapping segment can read their full word.
            words = [
                word
                for word in record["words"]
                if region[0] + 2 <= word["rect"][0] < word["rect"][2] <= region[2] - 2
            ]
            readings.append({**record, "words": words})
    if _ruler_strip(window, hwnd, bounds) != (parent, child, scroller, strip):
        raise RuntimeError("Playlist ruler geometry changed during OCR recovery")
    recovered = _merge_ruler_readings(records, readings, strip)
    grid = _calibrate(recovered, [], bounds, require_tracks=False)
    if not strip[1] <= grid.ruler_y < strip[3]:
        raise RuntimeError("OCR recovery did not establish the native Playlist ruler band")
    return recovered


def _scan(window, hwnd, region=None, tracks=None):
    from PIL import ImageGrab

    window.require_foreground()
    bounds = _scan_bounds(window.rect(hwnd), region)
    if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        raise RuntimeError("Requested Playlist OCR area is outside the window")
    picture = ImageGrab.grab(bbox=bounds, all_screens=True)
    records = _picture_records(picture, bounds)
    if region is None:
        class_name = window.class_name(hwnd)
        playlist = class_name == "TPlaylistForm" or (
            class_name == "TEventEditForm" and window.text(hwnd).casefold().startswith("playlist -")
        )
        if playlist:
            try:
                _calibrate(records, [], bounds, require_tracks=False)
            except RuntimeError:
                records = _recover_ruler(window, hwnd, picture, bounds, records)
    return _enhance_headers(picture, bounds, records, tracks) if tracks else records


@dataclass
class Grid:
    bounds: tuple
    bar_origin: float
    pixels_per_bar: float
    ruler_y: float
    track_rows: dict
    canvas_left: float | None = None
    canvas_boundary_source: str = "first_readable_ruler_label"

    def first_visible_bar(self):
        left = self.bounds[0] if self.canvas_left is None else self.canvas_left
        return max(1, math.ceil(1 + (left - self.bar_origin) / self.pixels_per_bar))

    def x(self, bar):
        x = self.bar_origin + (bar - 1) * self.pixels_per_bar
        left = self.bounds[0] if self.canvas_left is None else self.canvas_left
        if not left <= x < self.bounds[2] - 12:
            raise RuntimeError("Requested bar is not visible; scroll/zoom the Playlist and retry")
        return x

    def point(self, track, bar):
        if track not in self.track_rows:
            raise RuntimeError("Requested Playlist track label is not visible or uniquely readable")
        top, bottom = self.track_rows[track]
        return round(self.x(bar) + min(4, self.pixels_per_bar * 0.04)), round((top + bottom) / 2)


def _calibrate(records, tracks, bounds, require_tracks=True):
    """Infer a uniform bar ruler and explicit readable track rows, never guessed pixels."""
    numeric_rows = []
    for record in records:
        for word in record["words"]:
            text = word["text"]
            if not re.fullmatch(r"[1-9][0-9]{0,5}", text):
                continue
            rect = word["rect"]
            y = (rect[1] + rect[3]) / 2
            for row in numeric_rows:
                if abs(row[0] - y) < max(5, (rect[3] - rect[1]) / 2):
                    row[1].append((int(text), rect[0], rect))
                    break
            else:
                numeric_rows.append([y, [(int(text), rect[0], rect)]])
    candidates = []
    for y, row in numeric_rows:
        row.sort(key=lambda item: item[1])
        # Ruler labels increase strictly left to right; a numeric toolbar does not.
        if len(row) < 3 or any(b[0] <= a[0] for a, b in zip(row, row[1:])):
            continue
        slopes = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(row, row[1:])]
        slope = statistics.median(slopes)
        if not 12 <= slope <= 1500:
            continue
        origin = statistics.median(x - (bar - 1) * slope for bar, x, rect in row)
        if max(abs(x - (origin + (bar - 1) * slope)) for bar, x, rect in row) > max(
            4, slope * 0.025
        ):
            continue
        candidates.append((len(row), -y, origin, slope, y, min(item[1] for item in row)))
    if not candidates:
        raise RuntimeError("Cannot calibrate at least three bar labels; widen/zoom the Playlist")
    _, _, origin, slope, ruler_y, first_ruler_x = max(candidates)
    labels = []
    track_names = [_normal(track["name"]) for track in tracks]
    for track in tracks:
        # A repeated track name cannot establish which physical row owns the label.
        if track_names.count(_normal(track["name"])) != 1:
            continue
        possible = [
            record
            for record in records
            if _normal(record["text"]) == _normal(track["name"])
            and record["rect"][2] < first_ruler_x
            and record["rect"][1] > ruler_y + 6
        ]
        if len(possible) == 1:
            labels.append((track["index"], possible[0]["rect"]))
    labels.sort(key=lambda item: item[1][1])
    rows = {}
    # A following, consecutive track label establishes the row's actual lower edge.
    # Missing rows and the final partially visible row are never assigned guessed height.
    for (index, rect), (next_index, next_rect) in zip(labels, labels[1:]):
        if next_index != index + 1:
            continue
        top, bottom = rect[1] - 3, next_rect[1] - 3
        if bottom - top >= 14:
            rows[index] = (top, min(bottom, bounds[3] - 15))
    if require_tracks and not rows:
        raise RuntimeError("Keep two consecutive uniquely named Playlist track labels readable")
    return Grid(bounds, origin, slope, ruler_y, rows, first_ruler_x)


def _playlist(window):
    choices = []
    for class_name in ("TPlaylistForm", "TEventEditForm"):
        candidates = window.children(class_name=class_name) + window.windows(class_name=class_name)
        for hwnd in candidates:
            if class_name == "TPlaylistForm" or window.text(hwnd).casefold().startswith(
                "playlist -"
            ):
                choices.append(hwnd)
    choices = list(dict.fromkeys(choices))
    if len(choices) != 1:
        raise RuntimeError("Keep one unobstructed Playlist window visible")
    return choices[0]


def _native_canvas_boundary(window, hwnd, grid, records):
    """Prove the canvas edge from the Playlist's own horizontal ruler scroller."""
    labels = []
    for record in records:
        for word in record["words"]:
            rect = word["rect"]
            if (
                re.fullmatch(r"[1-9][0-9]{0,5}", word["text"])
                and abs((rect[1] + rect[3]) / 2 - grid.ruler_y) <= 8
                and abs(rect[0] - (grid.bar_origin + (int(word["text"]) - 1) * grid.pixels_per_bar))
                <= max(4, grid.pixels_per_bar * 0.025)
            ):
                labels.append(rect[0])
    if len(labels) < 3:
        return grid
    candidates = []
    bounds = window.rect(hwnd)
    for child in dict.fromkeys(window.children(parent=hwnd, class_name="TQuickScroller")):
        left, top, right, bottom = window.rect(child)
        width, height = right - left, bottom - top
        if (
            height <= 0
            or width / height <= 5
            or not (
                bounds[0] <= left < right <= bounds[2] and bounds[1] <= top < bottom <= bounds[3]
            )
            or not 0 <= grid.ruler_y - bottom <= max(48, height * 3)
            or sum(left <= x < right for x in labels) < 3
        ):
            continue
        candidates.append((child, (left, top, right, bottom)))
    if len(candidates) != 1:
        return grid
    child, rect = candidates[0]
    point = ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
    try:
        # Both checks use WindowFromPoint + IsChild. A scroller on another
        # editor or covered by another FL window cannot prove this boundary.
        window._check_point(hwnd, point)
        window._check_point(child, point)
    except RuntimeError:
        return grid
    grid.canvas_left = rect[0]
    grid.canvas_boundary_source = "playlist_horizontal_scroller"
    return grid


def _view(window, connection, prepared):
    hwnd = _playlist(window)
    bounds = _scan_bounds(window.rect(hwnd))
    records = _scan(window, hwnd, tracks=prepared["tracks"])
    grid = _calibrate(records, prepared["tracks"], bounds)
    grid = _native_canvas_boundary(window, hwnd, grid, records)
    source_names = prepared["source_names"]
    normalized = [_normal(name) for name in source_names]
    names = {_normal(name): name for name in source_names if normalized.count(_normal(name)) == 1}
    clips = []
    for record in records:
        name = names.get(_normal(record["text"]))
        rect = record["rect"]
        if name is None or rect[0] < grid.canvas_left:
            continue
        matching = [
            track
            for track, (top, bottom) in grid.track_rows.items()
            if top <= (rect[1] + rect[3]) / 2 < bottom
        ]
        if len(matching) != 1:
            continue
        clips.append(
            {
                "name": name,
                "track": matching[0],
                "rect": rect,
                "approximate_bar": 1 + (rect[0] - grid.bar_origin) / grid.pixels_per_bar,
            }
        )
    return hwnd, grid, clips, records


def _matches(clips, grid, name, track, bar):
    x = grid.x(bar)
    return [
        clip
        for clip in clips
        if clip["name"] == name
        and clip["track"] == track
        and x - 8 <= clip["rect"][0] <= x + min(80, grid.pixels_per_bar * 0.75)
    ]


def _locate(clips, grid, name, track, bar):
    matches = _matches(clips, grid, name, track, bar)
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one visible clip matching name, track and start bar")
    return matches[0]


def _target_name_records(records, name, start_x):
    matches = []
    for record in records:
        words = record["words"]
        for first in (0, 1):
            if first >= len(words):
                continue
            # FL draws one small source-type icon before the clip name. OCR may
            # report that icon as a character; never remove a real title prefix.
            if first and (len(words[0]["text"]) != 1 or words[0]["rect"][2] > start_x + 14):
                continue
            selected = words[first:]
            if _normal(" ".join(word["text"] for word in selected)) != _normal(name):
                continue
            rects = [word["rect"] for word in selected]
            matches.append(
                (
                    min(rect[0] for rect in rects),
                    min(rect[1] for rect in rects),
                    max(rect[2] for rect in rects),
                    max(rect[3] for rect in rects),
                )
            )
    return matches


def _target_records(window, hwnd, region):
    from PIL import ImageGrab

    window.require_foreground()
    window._check_point(hwnd, ((region[0] + region[2]) / 2, (region[1] + region[3]) / 2))
    picture = ImageGrab.grab(bbox=region, all_screens=True).convert("RGB")

    def visible(word):
        left, top, right, bottom = word["rect"]
        if not (
            all(math.isfinite(part) for part in word["rect"])
            and region[0] <= left < right <= region[2]
            and region[1] <= top < bottom <= region[3]
        ):
            return False
        local = (
            math.floor(left - region[0]),
            math.floor(top - region[1]),
            math.ceil(right - region[0]),
            math.ceil(bottom - region[1]),
        )
        # At least two original RGB colors are necessary for any visible glyph.
        # No contrast threshold is imposed on faint but nonflat text.
        return any(low != high for low, high in picture.crop(local).getextrema())

    result = []
    for record in _picture_records(picture, region, 2, 1.5) + _picture_records(
        picture, region, 2, 2
    ):
        words = [word for word in record["words"] if visible(word)]
        if words:
            rects = [word["rect"] for word in words]
            result.append(
                {
                    **record,
                    "words": words,
                    "text": " ".join(word["text"] for word in words),
                    "rect": (
                        min(rect[0] for rect in rects),
                        min(rect[1] for rect in rects),
                        max(rect[2] for rect in rects),
                        max(rect[3] for rect in rects),
                    ),
                }
            )
    return result


def _target_clip(window, hwnd, grid, clips, name, track, bar, required=True):
    matches = _matches(clips, grid, name, track, bar)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise RuntimeError("Multiple visible clip labels match the requested target")
    x = grid.x(bar)
    if track not in grid.track_rows:
        raise RuntimeError("Target track is not visibly calibrated")
    top, bottom = grid.track_rows[track]
    region = (
        math.floor(x),
        max(grid.bounds[1], math.floor(top - 8)),
        min(grid.bounds[2] - 12, math.ceil(x + min(400, max(267, len(name) * 12 + 32)))),
        min(grid.bounds[3] - 12, math.ceil(min(bottom, top + 46))),
    )
    rectangles = _target_name_records(_target_records(window, hwnd, region), name, x)
    rectangles = [
        rect
        for rect in rectangles
        if x - 8 <= rect[0] <= x + min(80, grid.pixels_per_bar * 0.75)
        and top <= (rect[1] + rect[3]) / 2 < bottom
    ]
    if rectangles:
        centers = [((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2) for rect in rectangles]
        if (
            max(point[0] for point in centers) - min(point[0] for point in centers) > 8
            or max(point[1] for point in centers) - min(point[1] for point in centers) > 8
        ):
            raise RuntimeError("Targeted OCR returned conflicting clip locations")
        rect = tuple(statistics.median(rect[index] for rect in rectangles) for index in range(4))
        return {
            "name": name,
            "track": track,
            "rect": rect,
            "approximate_bar": 1 + (rect[0] - grid.bar_origin) / grid.pixels_per_bar,
        }
    if required:
        raise RuntimeError("Expected exactly one readable target clip name at this track/bar")
    return None


def _move_pointer(window, hwnd, point):
    point = tuple(round(part) for part in point)
    window.require_foreground()
    window._check_point(hwnd, point)
    window.mouse.position = point
    window.require_foreground()


def _tile_region(window, hwnd, clip):
    bounds = window.rect(hwnd)
    rect = clip["rect"]
    region = (
        max(bounds[0], math.floor(rect[0] - 4)),
        max(bounds[1], math.floor(rect[1] - 4)),
        min(bounds[2], math.ceil(rect[2] + 4)),
        min(bounds[3], math.ceil(rect[3] + 4)),
    )
    if region[2] - region[0] < 8 or region[3] - region[1] < 8:
        raise RuntimeError("Clip label is too small for an independent selection witness")
    return region


def _capture_tile(window, hwnd, region):
    from PIL import ImageGrab

    window.require_foreground()
    window._check_point(hwnd, ((region[0] + region[2]) / 2, (region[1] + region[3]) / 2))
    picture = ImageGrab.grab(bbox=region, all_screens=True).convert("RGB")
    return picture.size, picture.tobytes()


def _stable_tile(window, hwnd, region):
    previous = None
    repeated = 0
    for unused in range(10):
        current = _capture_tile(window, hwnd, region)
        repeated = repeated + 1 if current == previous else 1
        if repeated == 3:
            return current
        previous = current
        time.sleep(0.08)
    raise RuntimeError("Clip pixels are not stable; selection/deletion cannot be verified")


def _selection_cycle(window, hwnd, clip, neutral_point):
    from pynput.keyboard import Key

    region = _tile_region(window, hwnd, clip)
    rect = clip["rect"]
    point = ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)

    def image():
        _move_pointer(window, hwnd, neutral_point)
        return _stable_tile(window, hwnd, region)

    def toggle():
        window._check_point(hwnd, point)
        # FL documents Ctrl+Shift-click for adding/removing individual clips.
        with window.keyboard.pressed(Key.ctrl, Key.shift):
            window.click(point)

    baseline = image()
    toggle()
    selected = image()
    if selected == baseline:
        raise RuntimeError("Clip did not show an independent selection change")
    toggle()
    if image() != baseline:
        raise RuntimeError("Clip selection change could not be reversed")
    toggle()
    if image() != selected:
        raise RuntimeError("Clip selection change could not be reproduced")
    return {
        "baseline": baseline,
        "selected": selected,
        "region": region,
        "neutral_point": neutral_point,
    }


def _select(window, connection, clip, expected_start=None, expected_end=None, witness_out=None):
    window.press("ctrl+d")
    # Clear stale time bounds and prove the clip changes selection reversibly
    # before Ctrl+Enter, which otherwise selects the entire zoomed viewport.
    cleared = clear_time_selection(connection, window)
    witness = _selection_cycle(window, _playlist(window), clip, cleared["ruler_point"])
    window.press("ctrl+enter")
    result = _command(connection, "arrangement.getSelection")
    start, end = result["start_ticks"], result["end_ticks"]
    if start < 0 or end <= start:
        raise RuntimeError("FL Studio did not confirm a nonempty selected clip")
    if expected_start is not None and start != expected_start:
        raise RuntimeError("Selected clip starts at a different tick; inspect grouping and retry")
    if expected_end is not None and end != expected_end:
        raise RuntimeError("Selected clip has a different length; inspect grouping and retry")
    if witness_out is not None:
        # Ctrl+Enter adds timeline shading. Bind the selected-state guard to a
        # fresh stable image with that shading, without further ruler actions.
        witness["selected_timeline"] = _stable_tile(window, _playlist(window), witness["region"])
        witness["window_rect"] = window.rect(_playlist(window))
        witness_out.update(witness)
    result["selection_witness"] = "reversible_stable_clip_pixels"
    return result


def _open_playlist_menu(window):
    hwnd = _playlist(window)
    captions = [
        child
        for child in window.children(parent=hwnd, class_name="TNewCaption")
        if window.text(child).casefold().startswith("playlist -")
    ]
    if len(captions) != 1:
        raise RuntimeError("Cannot identify the Playlist menu caption")
    left, top, right, bottom = window.rect(captions[0])
    if right - left < 80 or not 12 <= bottom - top <= 80:
        raise RuntimeError("Unexpected Playlist caption geometry")
    # Verified FL26 TNewCaption menu triangle, shared with the Channel Rack.
    point = (left + 10, (top + bottom) / 2)
    window._check_point(captions[0], point)
    window.click(point)


def _snap_bar(window, connection):
    # FPT_Menu opens FL's main File menu even while Playlist is focused.
    _open_playlist_menu(window)
    window.choose_menu("Snap")
    window.choose_menu("Bar")


def inspect_visible_clips(connection):
    prepared = _command(connection, "arrangement.preparePlaylist", {"scroll": False})
    with FLWindows() as window:
        hwnd, grid, clips, records = _view(window, connection, prepared)
        return {
            "success": True,
            "coverage": "visible_readable_labels_only",
            "clip_enumeration_complete": False,
            "clips": [
                {key: value for key, value in clip.items() if key != "rect"} for clip in clips
            ],
            "visible_tracks": sorted(grid.track_rows),
            "inspection_bounds": grid.bounds,
            "inspection_scope": "top_1100_pixels_of_visible_playlist",
            "canvas_boundary_source": grid.canvas_boundary_source,
            "index_base": 1,
            "notes": (
                "Only the top 1100 pixels are scanned. Truncated, hidden, stacked "
                "or unreadable clips may be omitted."
            ),
        }


def _reset_clip_selection(window, connection):
    # Unselected title colors are considerably more readable to Windows OCR.
    window.press("ctrl+d")
    clear_time_selection(connection, window)


def inspect_clip(connection, name, track, bar):
    prepared = _command(connection, "arrangement.preparePlaylist", {"track": track, "bar": bar})
    with FLWindows() as window:
        _reset_clip_selection(window, connection)
        hwnd, grid, clips, records = _view(window, connection, prepared)
        clip = _target_clip(window, hwnd, grid, clips, name, track, bar)
        bounds = _select(window, connection, clip, (bar - 1) * prepared["timing"]["ticks_per_bar"])
        return {
            "success": True,
            "name": name,
            "track": track,
            **bounds,
            "verification": "visible_identity_and_selected_time_bounds",
            "index_base": 1,
        }


def clear_time_selection(connection, windows=None):
    """Double-click the identified ruler, then confirm selection has no extent."""
    if windows is None:
        with FLWindows() as window:
            return clear_time_selection(connection, window)
    _command(connection, "arrangement.preparePlaylist", {"scroll": False})
    hwnd = _playlist(windows)
    records = _scan(windows, hwnd)
    grid = _calibrate(records, [], _scan_bounds(windows.rect(hwnd)), require_tracks=False)
    grid = _native_canvas_boundary(windows, hwnd, grid, records)
    visible_bar = grid.first_visible_bar()
    point = (grid.x(visible_bar) + 8, grid.ruler_y)
    windows.click(point, count=2)
    result = _command(connection, "arrangement.getSelection")
    if result["end_ticks"] > result["start_ticks"] >= 0:
        raise RuntimeError("Timeline selection was not cleared")
    return {"success": True, "status": "cleared", "ruler_point": point, **result}


def _picker_geometry(window, hwnd):
    bounds = window.rect(hwnd)
    captions = [
        child
        for child in window.children(parent=hwnd, class_name="TNewCaption")
        if window.text(child).casefold().startswith("playlist -")
    ]
    bars = [
        child
        for child in window.children(parent=hwnd, class_name="TNumBar")
        if 0 < window.rect(child)[2] - window.rect(child)[0] <= 32
        and window.rect(child)[3] - window.rect(child)[1] > 100
        and bounds[0] < window.rect(child)[0] < bounds[0] + 600
    ]
    if len(captions) != 1 or len(bars) != 1:
        raise RuntimeError("Keep one visible Playlist Picker sidebar and caption")
    caption, bar = window.rect(captions[0]), window.rect(bars[0])
    if not (60 <= bar[0] - bounds[0] <= 600 and 12 <= bar[1] - caption[3] <= 100):
        raise RuntimeError("Unexpected Picker sidebar header geometry")
    return bounds, caption, bar


def _set_picker_tab(window, connection, hwnd, kind):
    expected = {"pattern": "Picker: patterns", "audio": "Picker: audio clips"}.get(kind)
    if expected is None:
        raise ValueError("Unsupported Playlist source kind")
    geometry = _picker_geometry(window, hwnd)
    bounds, caption, bar = geometry
    fraction = 1 / 6 if kind == "pattern" else 1 / 2
    point = (bounds[0] + (bar[0] - bounds[0]) * fraction, (caption[3] + bar[1]) / 2)
    _move_pointer(window, hwnd, point)
    time.sleep(0.12)
    first = _command(connection, "arrangement.getHint")["hint"]
    time.sleep(0.08)
    second = _command(connection, "arrangement.getHint")["hint"]
    if first != expected or second != expected:
        raise RuntimeError("Picker tab hint did not match twice: " + repr((first, second)))
    if _picker_geometry(window, hwnd) != geometry:
        raise RuntimeError("Picker sidebar geometry changed before tab selection")
    window._check_point(hwnd, point)
    window.click(point)
    return bar


def _sidebar_source(window, connection, source):
    hwnd = _playlist(window)
    bar = _set_picker_tab(window, connection, hwnd, source["kind"])
    bounds, caption, current_bar = _picker_geometry(window, hwnd)
    if current_bar != bar:
        raise RuntimeError("Picker sidebar geometry changed before reading its source list")
    top = max(caption[3], bar[1] - 16)
    region = (bounds[0], top, bar[0], min(bar[3], bounds[1] + MAX_SCAN_HEIGHT))
    records = _scan(window, hwnd, region)
    records = [
        record
        for record in records
        if all(
            region[0] <= word["rect"][0] < word["rect"][2] <= region[2]
            and region[1] <= word["rect"][1] < word["rect"][3] <= region[3]
            for word in record["words"]
        )
    ]
    if source["kind"] == "pattern":
        # A single icon glyph may occupy only the sidebar's leftmost 16px.
        # Match all remaining source words exactly, with their original order.
        matches = _target_name_records(records, source["name"], region[0] + 2)
    else:
        matches = [
            record["rect"]
            for record in records
            if _normal(record["text"]) == _normal(source["name"])
        ]
    if len(matches) != 1:
        raise RuntimeError(
            "Keep exactly one fully readable source name in the Picker sidebar: " + source["name"]
        )
    rect = matches[0]
    point = ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
    window._check_point(hwnd, point)
    window.click(point)
    if source["kind"] == "pattern":
        actual = _command(connection, "arrangement.getPattern", {"index": source["index"]})[
            "pattern"
        ]
        if not actual["active"] or actual["name"] != source["name"]:
            raise RuntimeError("Picker did not activate the requested source pattern")


def place_clip(
    connection, track, bar, pattern_index=None, source_channel=None, allow_overlap=False
):
    if (pattern_index is None) == (source_channel is None):
        raise ValueError("Specify exactly one pattern or Audio Clip source")
    if type(allow_overlap) is not bool:
        raise ValueError("allow_overlap must be boolean")
    params = {"track": track, "bar": bar}
    if pattern_index is not None:
        params["pattern_index"] = pattern_index
    if source_channel is not None:
        params["source_channel"] = source_channel
    prepared = _command(connection, "arrangement.preparePlaylist", params)
    source = prepared["source"]
    if source is None:
        raise RuntimeError("FL Studio did not identify the requested clip source")
    if source.get("name_occurrences", 1) != 1:
        raise ValueError("Use a unique source name so the placed clip identity can be verified")
    ticks_per_bar = prepared["timing"]["ticks_per_bar"]
    with FLWindows() as window:
        _sidebar_source(window, connection, source)
        hwnd, grid, clips, records = _view(window, connection, prepared)
        point = grid.point(track, bar)
        nearby = [
            clip
            for clip in clips
            if clip["track"] == track and abs(clip["approximate_bar"] - bar) < 1
        ]
        if nearby and not allow_overlap:
            raise RuntimeError("A visible clip is near the destination; overlap was not authorized")
        _snap_bar(window, connection)
        hwnd, grid, clips, records = _view(window, connection, prepared)
        point = grid.point(track, bar)
        if not allow_overlap and any(
            clip["track"] == track and abs(clip["approximate_bar"] - bar) < 1 for clip in clips
        ):
            raise RuntimeError("A visible clip is near the destination; overlap was not authorized")
        window.click(point)
        _move_pointer(window, hwnd, (grid.x(bar) + 8, grid.ruler_y))
        time.sleep(0.15)
        hwnd, grid, clips, records = _view(window, connection, prepared)
        clip = _target_clip(window, hwnd, grid, clips, source["name"], track, bar)
        expected_end = None
        if source["kind"] == "pattern" and source["length_beats"] > 0:
            expected_end = (bar - 1) * ticks_per_bar + source["length_beats"] * prepared["timing"][
                "ppq"
            ]
        bounds = _select(window, connection, clip, (bar - 1) * ticks_per_bar, expected_end)
        return {
            "success": True,
            "status": "placed",
            "source": source,
            "track": track,
            "bar": bar,
            **bounds,
            "verification": "visible_identity_and_selected_time_bounds",
            "overlap_check": "visible_readable_labels_only",
            "snap_mode": "Bar",
            "index_base": 1,
        }


def _view_signature(window, hwnd, grid, tracks=None):
    rows = (
        grid.track_rows
        if tracks is None
        else {track: grid.track_rows[track] for track in tracks if track in grid.track_rows}
    )
    return (
        hwnd,
        window.rect(hwnd),
        grid.bounds,
        grid.bar_origin,
        grid.pixels_per_bar,
        grid.ruler_y,
        grid.canvas_left,
        tuple(sorted(rows.items())),
    )


def _same_view(before, after):
    # Native geometry must be exact. OCR ruler/label rectangles can shift by a
    # fraction of a pixel when time-selection shading changes glyph rendering.
    if any(before[index] != after[index] for index in (0, 1, 2, 6)):
        return False
    if (
        abs(before[3] - after[3]) > 2
        or abs(before[5] - after[5]) > 2
        or abs(before[4] - after[4]) > max(2, before[4] * 0.02)
    ):
        return False
    old, new = dict(before[7]), dict(after[7])
    return old.keys() == new.keys() and all(
        abs(old[track][edge] - new[track][edge]) <= 2 for track in old for edge in (0, 1)
    )


def _drag_points(grid, clip, source_track, source_bar, destination_track, destination_bar):
    grid.point(source_track, source_bar)
    grid.point(destination_track, destination_bar)
    rect = clip["rect"]
    start = ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
    end = (
        start[0] + grid.x(destination_bar) - grid.x(source_bar),
        # Preserve the grab offset from the track top. Center differences change
        # that offset for unequal heights and can leave the clip in its old row.
        # This correction is covered offline; its FL drop result awaits a live check.
        start[1] + grid.track_rows[destination_track][0] - grid.track_rows[source_track][0],
    )
    for point, track in ((start, source_track), (end, destination_track)):
        top, bottom = grid.track_rows[track]
        if not grid.canvas_left <= point[0] < grid.bounds[2] - 12 or not top <= point[1] < min(
            bottom, grid.bounds[3] - 12
        ):
            raise ValueError("Both clip grab points must remain inside the visible Playlist canvas")
    return start, end


def _assert_selected_source(window, hwnd, witness):
    if window.rect(hwnd) != witness["window_rect"]:
        raise RuntimeError("Source window geometry changed before drag")
    _move_pointer(window, hwnd, witness["neutral_point"])
    if _stable_tile(window, hwnd, witness["region"]) != witness["selected_timeline"]:
        raise RuntimeError("Source is no longer in the freshly witnessed selected state")


def _drag_clip(window, hwnd, start, end):
    from pynput.mouse import Button

    _move_pointer(window, hwnd, start)
    try:
        window.mouse.press(Button.left)
        for step in range(1, 17):
            point = (
                start[0] + (end[0] - start[0]) * step / 16,
                start[1] + (end[1] - start[1]) * step / 16,
            )
            _move_pointer(window, hwnd, point)
            time.sleep(0.015)
    finally:
        window.mouse.release(Button.left)


def move_clip(connection, name, source_track, source_bar, destination_track, destination_bar):
    for value in (source_track, source_bar, destination_track, destination_bar):
        if type(value) is not int or not 1 <= value <= 1000000:
            raise ValueError("Track/bar must be a positive integer no greater than 1000000")
    prepared = _command(
        connection, "arrangement.preparePlaylist", {"track": source_track, "bar": source_bar}
    )
    if destination_track not in {track["index"] for track in prepared["tracks"]}:
        raise ValueError("Destination Playlist track does not exist")
    ticks_per_bar = prepared["timing"]["ticks_per_bar"]
    with FLWindows() as window:
        _reset_clip_selection(window, connection)
        hwnd, grid, clips, records = _view(window, connection, prepared)
        clip = _target_clip(window, hwnd, grid, clips, name, source_track, source_bar)
        # Reject offscreen endpoints before even changing a clip selection.
        _drag_points(grid, clip, source_track, source_bar, destination_track, destination_bar)
        signature = _view_signature(window, hwnd, grid, (source_track, destination_track))
        _snap_bar(window, connection)
        hwnd, grid, clips, records = _view(window, connection, prepared)
        if not _same_view(
            signature, _view_signature(window, hwnd, grid, (source_track, destination_track))
        ):
            raise RuntimeError("Playlist geometry changed before drag; nothing was moved")
        clip = _target_clip(window, hwnd, grid, clips, name, source_track, source_bar)
        start, end = _drag_points(
            grid, clip, source_track, source_bar, destination_track, destination_bar
        )
        witness = {}
        before = _select(
            window, connection, clip, (source_bar - 1) * ticks_per_bar, witness_out=witness
        )
        _assert_selected_source(window, hwnd, witness)
        _drag_clip(window, hwnd, start, end)
        _reset_clip_selection(window, connection)
        hwnd, grid, clips, records = _view(window, connection, prepared)
        if not _same_view(
            signature, _view_signature(window, hwnd, grid, (source_track, destination_track))
        ):
            raise RuntimeError("Playlist geometry changed during drag; inspect before retrying")
        target = _target_clip(window, hwnd, grid, clips, name, destination_track, destination_bar)
        expected_start = (destination_bar - 1) * ticks_per_bar
        duration = before["end_ticks"] - before["start_ticks"]
        after = _select(window, connection, target, expected_start, expected_start + duration)
        if (source_track, source_bar) != (destination_track, destination_bar) and _matches(
            clips, grid, name, source_track, source_bar
        ):
            raise RuntimeError("A source clip label remains after drag; move was not verified")
        return {
            "success": True,
            "status": "moved",
            "name": name,
            "track": destination_track,
            "bar": destination_bar,
            **after,
            "verification": "direct_drag_and_selected_time_bounds",
            "snap_mode": "Bar",
            "index_base": 1,
            "coverage": "both_endpoints_visible",
        }


def delete_clip(connection, name, track, bar, confirm):
    if confirm is not True:
        raise ValueError("Deleting a clip requires confirm=True")
    prepared = _command(connection, "arrangement.preparePlaylist", {"track": track, "bar": bar})
    with FLWindows() as window:
        _reset_clip_selection(window, connection)
        hwnd, grid, clips, records = _view(window, connection, prepared)
        signature = _view_signature(window, hwnd, grid, (track,))
        clip = _target_clip(window, hwnd, grid, clips, name, track, bar)
        witness = {}
        _select(
            window,
            connection,
            clip,
            (bar - 1) * prepared["timing"]["ticks_per_bar"],
            witness_out=witness,
        )
        # Preserve the freshly witnessed selected state until target right-click.
        _assert_selected_source(window, hwnd, witness)
        hwnd, grid, clips, records = _view(window, connection, prepared)
        if not _same_view(signature, _view_signature(window, hwnd, grid, (track,))):
            raise RuntimeError("Playlist geometry changed before deletion; nothing was deleted")
        _assert_selected_source(window, hwnd, witness)
        rect = clip["rect"]
        point = ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
        window._check_point(hwnd, point)
        # Draw-mode right-click targets this clip; no global Delete shortcut is used.
        window.click(point, button="right")
        # Only now remove range shading, so absence is not a shading-only change.
        clear_time_selection(connection, window)
        _move_pointer(window, hwnd, witness["neutral_point"])
        previous = None
        for unused in range(3):
            time.sleep(0.15)
            hwnd, grid, clips, records = _view(window, connection, prepared)
            if not _same_view(signature, _view_signature(window, hwnd, grid, (track,))):
                raise RuntimeError(
                    "Playlist geometry changed after deletion; inspect before retrying"
                )
            if _target_clip(window, hwnd, grid, clips, name, track, bar, required=False):
                raise RuntimeError(
                    "Target clip remains visible or ambiguous; deletion was not verified"
                )
            image = _stable_tile(window, hwnd, witness["region"])
            if image in (witness["baseline"], witness["selected"], witness["selected_timeline"]):
                raise RuntimeError(
                    "Target pixels are unchanged or merely deselected; deletion unverified"
                )
            if previous is not None and image != previous:
                raise RuntimeError("Target pixels are not stable after deletion")
            previous = image
        return {
            "success": True,
            "status": "deleted",
            "name": name,
            "track": track,
            "bar": bar,
            "verification": "three_label_absences_and_stable_changed_pixels",
            "coverage": "visible_only",
            "index_base": 1,
        }


def _confirm_pattern_delete(window):
    dialogs = [
        hwnd
        for hwnd in window.windows()
        if hwnd != window.root and window.class_name(hwnd) in ("TMsgForm", "#32770", "TMessageForm")
    ]
    for hwnd in dialogs:
        lines = _scan(window, hwnd)
        text = " ".join(record["text"] for record in lines).casefold()
        if "pattern" not in text or "delet" not in text:
            continue
        # FL26 uses OK/Cancel; Windows OCR reads its custom OK glyph as "0k".
        labels = [
            record for record in lines if _normal(record["text"]) in ("yes", "delete", "ok", "0k")
        ]
        if len(labels) != 1:
            raise RuntimeError("Pattern deletion confirmation is ambiguous")
        rect = labels[0]["rect"]
        point = ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
        controls = [
            child
            for child in window.children(hwnd)
            if window.class_name(child) in ("TQuickFocusBtn", "Button")
            and window.rect(child)[0] <= point[0] < window.rect(child)[2]
            and window.rect(child)[1] <= point[1] < window.rect(child)[3]
        ]
        if len(controls) != 1:
            raise RuntimeError(
                "Pattern confirmation label does not identify a unique native button"
            )
        window._check_point(controls[0], point)
        window.click(point)
        return


def delete_pattern(connection, pattern_index, expected_name, confirm):
    if confirm is not True:
        raise ValueError("Deleting a pattern requires confirm=True")
    prepared = _command(
        connection,
        "arrangement.preparePatternDelete",
        {"index": pattern_index, "expected_name": expected_name},
    )
    if not prepared["pattern"]["active"] or prepared["pattern"]["index"] != pattern_index:
        raise RuntimeError("FL Studio did not select the pattern deletion target")
    with FLWindows() as window:
        left, top, right, bottom = window.rect(window.root)
        records = _scan(window, window.root, (left, top, min(right, left + 1100), top + 100))
        words = [
            word
            for record in records
            for word in record["words"]
            if _normal(word["text"]) == "patterns"
        ]
        if len(words) != 1:
            raise RuntimeError("Cannot read the main PATTERNS menu uniquely")
        rect = words[0]["rect"]
        window.click(((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2))
        window.choose_menu("Delete")
        time.sleep(0.1)
        _confirm_pattern_delete(window)
    result = _command(
        connection,
        "arrangement.verifyPatternDelete",
        {"before_count": prepared["modified_pattern_count"]},
    )
    result.update(index=pattern_index, expected_name=expected_name)
    return result
