"""Offline toolbar/hint tests: no native windows, MIDI ports or input events."""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from fl_studio_mcp.utils import tempo_windows as native

TEMPO_RECT = (100, 10, 140, 30)
MEMORY_RECT = (300, 10, 340, 30)


def word(text, rect=TEMPO_RECT):
    return {"text": text, "rect": rect}


class Window:
    root = 1

    def __init__(self):
        self.mouse = SimpleNamespace(position=(800, 500))
        self.classes = {1: "TFruityLoopsMainForm", 2: "TQuickEditToolBar"}
        self.bounds = {1: (0, 0, 1000, 700), 2: (0, 0, 600, 80)}
        self.focused = True
        self.covered = False
        self.checked = []
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *args):
        self.exited = True

    def children(self, parent):
        if parent == self.root:
            return [hwnd for hwnd in self.classes if hwnd != self.root]
        return []

    def class_name(self, hwnd):
        return self.classes[hwnd]

    def rect(self, hwnd):
        return self.bounds[hwnd]

    def require_foreground(self):
        if not self.focused:
            raise RuntimeError("Foreground changed")

    def _check_point(self, hwnd, point):
        self.checked.append((hwnd, point))
        if self.covered:
            raise RuntimeError("Toolbar is covered")


class Connection:
    def __init__(self, window, timing=None, hints=None):
        self.window = window
        self.timing = timing or {"quarter_bpm": 70.0, "denominator": 8}
        self.hints = hints or {(120, 20): "Tempo: 140.000 BPM"}
        self.calls = []
        self.locked = 0
        self.after_call = None

    @contextmanager
    def transaction(self):
        self.locked += 1
        try:
            yield
        finally:
            self.locked -= 1

    def send_command(self, action, params, timeout):
        assert self.locked
        assert params == {} and timeout == 5
        self.calls.append(action)
        if action == "arrangement.getTiming":
            result = {"success": True, **self.timing}
        elif action == "system.getHint":
            hint = self.hints.get(tuple(self.window.mouse.position), "Memory: 140 MB")
            result = {"success": True, "hint": hint() if callable(hint) else hint}
        else:
            raise AssertionError("Read-only tempo must not send " + action)
        if self.after_call:
            self.after_call(action)
        return result


@pytest.fixture
def setup(monkeypatch):
    window = Window()
    connection = Connection(window)
    records = {2: [{"text": "Memory CPU 140.000", "words": [word("140.000")]}]}
    scans = []

    def scan(active, hwnd, region):
        assert active is window and region == window.bounds[hwnd]
        scans.append(hwnd)
        return records.get(hwnd, [])

    monkeypatch.setattr(native, "_scan", scan)
    monkeypatch.setattr(native, "_narrow_records", lambda *args: [])
    monkeypatch.setattr(native.time, "sleep", lambda value: None)
    return window, connection, records, scans


def test_six_eight_reads_displayed_140_from_quarter_70_and_restores_pointer(setup):
    window, connection, _, scans = setup
    result = native.read_display_tempo(connection, window)
    assert result == {
        "bpm": 140,
        "quarter_bpm": 70,
        "tempo_unit": "displayed_bpm",
        "ui_to_quarter_ratio": 2,
        "verification": "toolbar_ocr_word_and_stable_tempo_hover_hint",
    }
    assert window.mouse.position == (800, 500)
    assert scans == [2]
    assert connection.calls == [
        "arrangement.getTiming",
        "system.getHint",
        "system.getHint",
        "arrangement.getTiming",
    ]


def test_memory_counter_matching_the_ratio_is_not_tempo(setup):
    window, connection, records, _ = setup
    records[2][0]["words"].append(word("140", MEMORY_RECT))
    result = native.read_display_tempo(connection, window)
    assert result["bpm"] == 140
    assert connection.calls.count("system.getHint") == 4


def test_memory_only_candidate_is_rejected_and_pointer_restored(setup):
    window, connection, records, _ = setup
    records[2] = [{"text": "Tempo 140", "words": [word("140", MEMORY_RECT)]}]
    with pytest.raises(RuntimeError, match="exactly one"):
        native.read_display_tempo(connection, window)
    assert window.mouse.position == (800, 500)


def test_whole_line_number_does_not_become_a_word_candidate(setup):
    window, connection, records, _ = setup
    records[2] = [{"text": "140", "words": [word("Memory")]}]
    with pytest.raises(RuntimeError, match="numeric toolbar candidates"):
        native.read_display_tempo(connection, window)


@pytest.mark.parametrize("text", ["9.999", "522.001", "140MB", "1:40", "140.0000", "1e2"])
def test_only_bounded_plain_decimal_words_are_candidates(setup, text):
    window, connection, records, _ = setup
    records[2] = [{"words": [word(text)]}]
    with pytest.raises(RuntimeError, match="numeric toolbar candidates"):
        native.read_display_tempo(connection, window)


@pytest.mark.parametrize("ratio", [0.25, 0.5, 1, 2, 4, 8])
def test_supported_meter_ratios_when_denominator_is_unavailable(setup, ratio):
    window, connection, records, _ = setup
    value = 40 * ratio
    connection.timing = {"quarter_bpm": 40, "denominator": None}
    records[2] = [{"words": [word(str(value))]}]
    assert native.read_display_tempo(connection, window)["ui_to_quarter_ratio"] == ratio


def test_known_denominator_rejects_an_otherwise_legal_ratio(setup):
    window, connection, records, _ = setup
    records[2] = [{"words": [word("70")]}]
    with pytest.raises(RuntimeError, match="numeric toolbar candidates"):
        native.read_display_tempo(connection, window)


def test_legacy_quarter_bpm_field_and_three_decimal_precision(setup):
    window, connection, records, _ = setup
    connection.timing = {"bpm": 70.1235, "denominator": 8}
    records[2] = [{"words": [word("140.247")]}]
    assert native.read_display_tempo(connection, window)["bpm"] == 140.247


def test_identical_boxes_from_nested_toolbar_scans_are_deduplicated(setup):
    window, connection, records, _ = setup
    window.classes[3] = "TQuickEditToolbarPanel"
    window.bounds[3] = window.bounds[2]
    records[3] = records[2]
    original = window.children
    window.children = lambda parent: [3] if parent == 2 else original(parent)
    native.read_display_tempo(connection, window)
    assert connection.calls.count("system.getHint") == 2


def test_two_distinct_tempo_hint_boxes_are_ambiguous(setup):
    window, connection, records, _ = setup
    records[2][0]["words"].append(word("140", MEMORY_RECT))
    connection.hints[(320, 20)] = "Tempo"
    with pytest.raises(RuntimeError, match="exactly one"):
        native.read_display_tempo(connection, window)
    assert window.mouse.position == (800, 500)


@pytest.mark.parametrize("hint", ["TempoMemory", "Attempted read", "Memory: 140 MB"])
def test_hint_requires_an_independent_tempo_word(setup, hint):
    window, connection, _, _ = setup
    connection.hints[(120, 20)] = hint
    with pytest.raises(RuntimeError, match="exactly one"):
        native.read_display_tempo(connection, window)


def test_obstruction_is_rejected_before_ocr(setup):
    window, connection, _, scans = setup
    window.covered = True
    with pytest.raises(RuntimeError, match="covered"):
        native.read_display_tempo(connection, window)
    assert not scans
    assert window.mouse.position == (800, 500)


def test_foreground_loss_during_hover_is_rejected_and_pointer_restored(setup):
    window, connection, _, _ = setup
    connection.after_call = lambda action: (
        setattr(window, "focused", False) if (action == "system.getHint") else None
    )
    with pytest.raises(RuntimeError, match="Foreground"):
        native.read_display_tempo(connection, window)
    assert window.mouse.position == (800, 500)


def test_child_toolbar_geometry_change_after_hint_is_rejected(setup):
    window, connection, _, _ = setup
    count = 0

    def change(action):
        nonlocal count
        if action == "system.getHint":
            count += 1
            if count == 2:
                window.bounds[2] = (0, 0, 601, 80)

    connection.after_call = change
    with pytest.raises(RuntimeError, match="geometry"):
        native.read_display_tempo(connection, window)
    assert window.mouse.position == (800, 500)


def test_tempo_change_during_read_is_rejected(setup):
    window, connection, _, _ = setup
    connection.after_call = lambda action: (
        connection.timing.update(quarter_bpm=71) if (action == "system.getHint") else None
    )
    with pytest.raises(RuntimeError, match="tempo or toolbar"):
        native.read_display_tempo(connection, window)


def test_default_context_activates_standard_fl_window_and_closes(monkeypatch, setup):
    window, connection, _, _ = setup
    monkeypatch.setattr(native, "FLWindows", lambda: window)
    assert native.read_display_tempo(connection)["bpm"] == 140
    assert window.entered and window.exited


def test_parent_toolbar_is_not_scanned_over_its_matching_leaf_controls(setup):
    window, connection, records, scans = setup
    window.classes[3] = "TQuickEditToolbarPanel"
    window.bounds[3] = (90, 0, 150, 40)
    records[3] = records[2]
    original = window.children
    window.children = lambda parent: [3] if parent == 2 else original(parent)
    assert native.read_display_tempo(connection, window)["bpm"] == 140
    assert scans == [3]


def test_repeated_raw_word_records_in_the_same_box_are_one_candidate(setup):
    window, connection, records, _ = setup
    records[2].append({"words": [word("140.000")]})
    assert native.read_display_tempo(connection, window)["bpm"] == 140
    assert connection.calls.count("system.getHint") == 2


def test_narrow_word_fallback_reads_a_value_missed_beside_transport_graphics(setup, monkeypatch):
    window, connection, records, _ = setup
    records[2] = [{"text": "SONG", "words": [word("SONG")]}]
    monkeypatch.setattr(
        native,
        "_narrow_records",
        lambda *args: [{"words": [word("140.000"), word("140.000", (101, 10, 141, 30))]}],
    )
    assert native.read_display_tempo(connection, window)["bpm"] == 140
    assert connection.calls.count("system.getHint") == 2


def test_narrow_regions_stay_inside_native_panel_and_include_rightmost_digits():
    bounds = (341, 0, 537, 41)
    regions = native._narrow_regions(bounds)
    assert regions[-1] == (473, 0, 537, 41)
    assert all(
        bounds[0] <= left < right <= bounds[2]
        and bounds[1] <= top < bottom <= bounds[3]
        and right - left <= 64
        and bottom - top <= 64
        for left, top, right, bottom in regions
    )


def _diagnostic(error):
    return json.loads(str(error.value).split("diagnostic=", 1)[1])


def test_unique_hint_failure_reports_two_bounded_native_hints(setup):
    window, connection, _, _ = setup
    connection.hints[(120, 20)] = "Memory: " + "x" * 200 + "\nother text"
    with pytest.raises(RuntimeError, match="exactly one") as error:
        native.read_display_tempo(connection, window)
    diagnostic = _diagnostic(error)
    assert diagnostic["candidate_count"] == 1 and diagnostic["confirmed_count"] == 0
    candidate = diagnostic["candidates"][0]
    assert candidate["bpm"] == 140 and candidate["ratio"] == 2
    assert candidate["rect"] == list(TEMPO_RECT)
    assert all(len(hint) <= 96 and "\n" not in hint for hint in candidate["hints"])
    assert candidate["hint_stable"] and candidate["tempo_words"] == [False, False]
    assert not candidate["accepted"]


def test_tempo_hint_text_drift_is_explained_without_weakening_acceptance(setup):
    window, connection, _, _ = setup
    hints = iter(["Tempo: 140.000", "Tempo"])
    connection.hints[(120, 20)] = lambda: next(hints)
    with pytest.raises(RuntimeError, match="exactly one") as error:
        native.read_display_tempo(connection, window)
    candidate = _diagnostic(error)["candidates"][0]
    assert candidate["hints"] == ["Tempo: 140.000", "Tempo"]
    assert not candidate["hint_stable"] and candidate["tempo_words"] == [True, True]
    assert not candidate["accepted"]


def test_partial_160_and_full_160_decimal_word_are_same_native_control(setup):
    window, connection, records, _ = setup
    connection.timing = {"quarter_bpm": 80, "denominator": 8}
    records[2] = [{"words": [word("160", (100, 10, 122, 30)), word("160.000")]}]
    connection.hints[(111, 20)] = "Tempo: 160.000"
    result = native.read_display_tempo(connection, window)
    assert result["bpm"] == 160 and result["ui_to_quarter_ratio"] == 2
    assert connection.calls.count("system.getHint") == 2


def test_distinct_native_controls_are_never_merged_by_overlapping_screen_boxes(setup):
    window, connection, records, _ = setup
    window.classes[3] = "TQuickEditToolbarPanel"
    window.bounds[3] = window.bounds[2]
    records[3] = records[2]
    with pytest.raises(RuntimeError, match="exactly one") as error:
        native.read_display_tempo(connection, window)
    diagnostic = _diagnostic(error)
    assert diagnostic["candidate_count"] == 2 and diagnostic["confirmed_count"] == 2
    assert {item["hwnd"] for item in diagnostic["candidates"]} == {2, 3}


def test_enlarged_pixel_fallback_recognizes_digits_without_letter_coercion(setup, monkeypatch):
    window, connection, records, _ = setup
    connection.timing = {"quarter_bpm": 80, "denominator": 8}
    connection.hints[(120, 20)] = "Tempo: 160.000 BPM"
    records[2] = [{"words": [word("SONG")]}]
    scales = []

    def narrow(active, hwnd, bounds, pre_scale=1):
        scales.append(pre_scale)
        return [{"words": [word("160J00" if pre_scale == 1 else "160.000")]}]

    monkeypatch.setattr(native, "_narrow_records", narrow)
    result = native.read_display_tempo(connection, window)
    assert scales == [1, 2]
    assert result["bpm"] == 160 and result["ui_to_quarter_ratio"] == 2
    assert connection.calls.count("system.getHint") == 2
    assert window.mouse.position == (800, 500)


def test_enlarged_fallback_still_rejects_letters_without_hovering(setup, monkeypatch):
    window, connection, records, _ = setup
    connection.timing = {"quarter_bpm": 80, "denominator": 8}
    records[2] = [{"words": [word("160mo:")]}]
    monkeypatch.setattr(native, "_narrow_records", lambda *args: [{"words": [word("160J00")]}])
    with pytest.raises(RuntimeError, match="numeric toolbar candidates"):
        native.read_display_tempo(connection, window)
    assert "system.getHint" not in connection.calls
    assert window.mouse.position == (800, 500)


def test_enlarged_fallback_never_bypasses_tempo_hint(setup, monkeypatch):
    window, connection, records, _ = setup
    connection.timing = {"quarter_bpm": 80, "denominator": 8}
    connection.hints[(120, 20)] = "Memory: 160 MB"
    records[2] = []
    monkeypatch.setattr(
        native,
        "_narrow_records",
        lambda *args: [{"words": [word("160.000")]}] if args[-1] == 2 else [],
    )
    with pytest.raises(RuntimeError, match="exactly one"):
        native.read_display_tempo(connection, window)
    assert window.mouse.position == (800, 500)


def test_fallback_counts_both_scales_toward_one_total_budget(setup, monkeypatch):
    window, connection, records, _ = setup
    records[2] = []
    monkeypatch.setattr(native, "_narrow_regions", lambda bounds: [bounds] * 129)
    scales = []
    monkeypatch.setattr(native, "_narrow_records", lambda *args: scales.append(args[-1]) or [])
    with pytest.raises(RuntimeError, match="total bounded budget"):
        native.read_display_tempo(connection, window)
    assert scales == [1]
    assert "system.getHint" not in connection.calls
