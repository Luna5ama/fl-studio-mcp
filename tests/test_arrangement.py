"""Exercise production arrangement code against explicit FL and OCR boundaries."""

import importlib.util
import math
import sys
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from fl_studio_mcp.tools import arrangement as tools
from fl_studio_mcp.utils import arrangement_windows as native

ROOT = Path(__file__).resolve().parents[1]


class Patterns:
    def __init__(self):
        self.active = 1
        self.records = {2: {"name": "Bass", "length": 4, "notes": [36, 43]}}
        self.calls = []
        self.reject_jump = False
        self.reject_length = False

    def patternMax(self):
        return 8

    def patternNumber(self):
        return self.active

    def patternCount(self):
        return len(self.records)

    def isPatternDefault(self, index):
        return index not in self.records

    def getPatternName(self, index):
        return self.records.get(index, {}).get("name", "Pattern " + str(index))

    def getPatternLength(self, index):
        return self.records.get(index, {}).get("length", 4)

    def getPatternColor(self, index):
        return 0

    def isPatternSelected(self, index):
        return self.active == index

    def jumpToPattern(self, index):
        self.calls.append(("jump", index))
        if not self.reject_jump:
            self.active = index

    def setPatternName(self, index, name):
        self.calls.append(("name", index, name))
        self.records.setdefault(index, {"length": 4, "notes": []})["name"] = name

    def setPatternLength(self, index, length):
        self.calls.append(("length", index, length))
        if not self.reject_length:
            self.records[index]["length"] = length

    def deselectAll(self):
        self.calls.append(("deselect",))

    def clonePattern(self, source, destination):
        self.calls.append(("clone", source, destination))
        self.records[destination] = deepcopy(self.records[source])


class Host:
    def __init__(self):
        self.patterns = Patterns()
        self.numerator = 4
        self.denominator = 4
        self.bpm = 140.0
        self.version = 45
        self.playing = False
        self.recording = False
        self.ready = True
        self.reject_tempo = False
        self.inverted_tempo_mode = False
        self.reject_track = False
        self.calls = []
        self.track_names = {i: "Track " + str(i) for i in range(1, 5)}
        self.muted = {i: False for i in self.track_names}
        self.channel_names = ["Drum", "Test audio"]
        self.channel_types = [0, 4]

    def tempo(self, as_int):
        quarter = self.bpm * 4 / self.denominator
        if self.inverted_tempo_mode:
            return quarter if as_int else int(round(quarter * 1000))
        return int(round(quarter)) if as_int else quarter

    def set_tempo(self, bpm, as_int):
        assert as_int is self.inverted_tempo_mode
        self.calls.append(("tempo", bpm))
        if not self.reject_tempo:
            self.bpm = bpm

    def set_numerator(self, value):
        self.calls.append(("numerator", value))
        self.numerator = value

    def set_denominator(self, value):
        self.calls.append(("denominator", value))
        self.denominator = value

    def set_track_name(self, index, name):
        self.calls.append(("track_name", index, name))
        if not self.reject_track:
            self.track_names[index] = name

    def mute_track(self, index, muted):
        self.calls.append(("mute", index, muted))
        if not self.reject_track:
            self.muted[index] = bool(muted)

    def call(self, name):
        return lambda *args: self.calls.append((name, *args))

    def modules(self):
        return {
            "patterns": self.patterns,
            "arrangement": SimpleNamespace(selectionStart=lambda: -1, selectionEnd=lambda: -1),
            "channels": SimpleNamespace(
                channelCount=lambda global_index: len(self.channel_names),
                getChannelName=lambda index, global_index: self.channel_names[index],
                getChannelType=lambda index, global_index: self.channel_types[index],
                selectOneChannel=self.call("channel_select"),
            ),
            "general": SimpleNamespace(
                getRecPPQ=lambda: 96,
                getRecPPB=lambda: 96 * self.numerator * 4 // self.denominator,
                getVersion=lambda: self.version,
                safeToEdit=lambda: self.ready,
                setNumerator=self.set_numerator,
                setDenominator=self.set_denominator,
            ),
            "midi": SimpleNamespace(widPlaylist=2, FPT_Menu=90, PME_System=2, PME_FromMIDI=32),
            "mixer": SimpleNamespace(getCurrentTempo=self.tempo, setCurrentTempo=self.set_tempo),
            "playlist": SimpleNamespace(
                trackCount=lambda: len(self.track_names),
                getPerformanceModeState=lambda: False,
                getTrackName=self.track_names.__getitem__,
                getTrackColor=lambda index: 0,
                isTrackMuted=self.muted.__getitem__,
                isTrackSelected=lambda index: False,
                setTrackName=self.set_track_name,
                muteTrack=self.mute_track,
                selectTool=self.call("tool"),
            ),
            "transport": SimpleNamespace(
                isPlaying=lambda: self.playing,
                isRecording=lambda: self.recording,
                globalTransport=self.call("transport"),
            ),
            "ui": SimpleNamespace(
                showWindow=self.call("show"),
                setFocused=self.call("focus"),
                scrollWindow=self.call("scroll"),
                getHintMsg=lambda: "test hint",
            ),
        }


@pytest.fixture
def controller(monkeypatch):
    host = Host()
    for name, module in host.modules().items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location(
        "arrangement_under_test", ROOT / "fl_controller/mcp_arrangement.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, host


def test_namespace_and_pattern_listing_do_not_edit(controller):
    module, host = controller
    assert module.dispatch("routing.get", {}) is None
    result = module.dispatch("arrangement.getPatterns", {})
    assert [p["index"] for p in result["patterns"]] == [1, 2]
    assert result["modified_pattern_count"] == 1
    assert (
        len(module.dispatch("arrangement.getPatterns", {"include_defaults": True})["patterns"]) == 8
    )
    assert host.calls == host.patterns.calls == []


def test_create_allocates_unused_slot_and_preserves_existing_data(controller):
    module, host = controller
    original = deepcopy(host.patterns.records[2])
    result = module.dispatch("arrangement.createPattern", {"name": "Lead", "length_beats": 8})
    assert result["pattern"]["index"] == 3
    assert result["pattern"]["active"] and result["pattern"]["length_beats"] == 8
    assert host.patterns.records[2] == original
    assert 1 not in host.patterns.records  # Active default is not silently renamed.


@pytest.mark.parametrize(
    "params",
    [
        {"name": ""},
        {"name": " "},
        {"name": "a" * 256},
        {"name": "x", "length_beats": True},
        {"name": "x", "length_beats": 0},
        {"name": "x", "index": 2},
        {"name": "x", "index": 1},
        {"name": "x", "index": 9},
    ],
)
def test_create_rejects_invalid_or_used_slot_without_mutation(controller, params):
    module, host = controller
    with pytest.raises(ValueError):
        module.dispatch("arrangement.createPattern", params)
    assert host.patterns.calls == []


def test_create_verifies_requested_length(controller):
    module, host = controller
    host.patterns.reject_length = True
    with pytest.raises(RuntimeError, match="create/select"):
        module.dispatch("arrangement.createPattern", {"name": "Lead", "length_beats": 8})


def test_clone_copies_real_data_to_explicit_unused_destination(controller):
    module, host = controller
    source = deepcopy(host.patterns.records[2])
    result = module.dispatch("arrangement.clonePattern", {"index": 2, "destination": 4})
    assert result["pattern"]["index"] == 4
    assert result["pattern"]["name"] == "Bass copy"
    assert host.patterns.records[4]["notes"] == source["notes"]
    assert host.patterns.records[4]["notes"] is not host.patterns.records[2]["notes"]
    assert host.patterns.records[2] == source
    assert result["verification"] == "destination_identity_and_length"


def test_clone_rejects_older_api_before_writing(controller):
    module, host = controller
    host.version = 42
    with pytest.raises(RuntimeError, match="43"):
        module.dispatch("arrangement.clonePattern", {"index": 2})
    assert host.patterns.calls == []


def test_delete_pattern_requires_current_identity_and_successful_selection(controller):
    module, host = controller
    with pytest.raises(ValueError, match="identity"):
        module.dispatch("arrangement.preparePatternDelete", {"index": 2, "expected_name": "Old"})
    assert host.patterns.calls == []
    host.patterns.reject_jump = True
    with pytest.raises(RuntimeError, match="could not be selected"):
        module.dispatch("arrangement.preparePatternDelete", {"index": 2, "expected_name": "Bass"})
    assert host.patterns.records[2]["name"] == "Bass"


def test_delete_pattern_does_not_claim_clear_is_true_deletion(controller):
    module, host = controller
    result = module.dispatch("arrangement.verifyPatternDelete", {"before_count": 2})
    assert result["success"] and result["status"] == "deleted"
    result = module.dispatch("arrangement.verifyPatternDelete", {"before_count": 1})
    assert not result["success"] and result["status"] == "unknown"
    assert "do not retry" in result["error"]


@pytest.mark.parametrize("bpm", [math.nan, math.inf, -math.inf, 0, -1, 2088.25, True, "140"])
def test_invalid_tempo_is_rejected_before_mutation(controller, bpm):
    module, host = controller
    with pytest.raises(ValueError):
        module.dispatch("arrangement.setTempo", {"display_bpm": bpm, "quarter_bpm": 140})
    assert host.calls == []


def test_tempo_explicit_float_mode_and_readback(controller):
    module, host = controller
    result = module.dispatch("arrangement.setTempo", {"display_bpm": 123.45, "quarter_bpm": 123.45})
    assert result["quarter_bpm"] == 123.45 and result["ticks_per_bar"] == 384
    host.reject_tempo = True
    with pytest.raises(RuntimeError, match="readback"):
        module.dispatch("arrangement.setTempo", {"display_bpm": 120, "quarter_bpm": 120})


def test_time_signature_reports_missing_denominator_getter(controller):
    module, host = controller
    result = module.dispatch("arrangement.setTimeSignature", {"numerator": 3, "denominator": 8})
    assert host.denominator == 8
    assert result["numerator"] is None and result["ticks_per_bar"] == 144
    assert result["quarter_beats_per_bar"] == 1.5
    assert not result["numerator_readback_supported"]
    assert result["denominator"] is None
    assert not result["denominator_readback_supported"]
    assert result["denominator_requested"] == 8


@pytest.mark.parametrize("denominator", [0, 3, True, 64, "4"])
def test_invalid_time_signature_has_no_side_effects(controller, denominator):
    module, host = controller
    with pytest.raises(ValueError):
        module.dispatch(
            "arrangement.setTimeSignature", {"numerator": 3, "denominator": denominator}
        )
    assert host.calls == []


def test_queries_work_during_playback_but_mutations_are_refused(controller):
    module, host = controller
    host.playing = True
    assert module.dispatch("arrangement.getTiming", {})["quarter_bpm"] == 140
    with pytest.raises(RuntimeError, match="Stop playback"):
        module.dispatch("arrangement.renamePattern", {"index": 2, "name": "New"})
    assert host.patterns.calls == []


def test_track_prevalidates_all_fields_then_reads_back(controller):
    module, host = controller
    with pytest.raises(ValueError, match="boolean"):
        module.dispatch("arrangement.setTrack", {"track": 1, "name": "New", "muted": 1})
    assert host.calls == [] and host.track_names[1] == "Track 1"
    result = module.dispatch("arrangement.setTrack", {"track": 1, "name": "New", "muted": True})
    assert result["track"]["name"] == "New" and result["track"]["muted"]
    host.reject_track = True
    with pytest.raises(RuntimeError, match="readback"):
        module.dispatch("arrangement.setTrack", {"track": 1, "muted": False})


def test_playlist_metadata_preserves_one_based_tracks_and_discloses_clip_limit(controller):
    module, host = controller
    result = module.dispatch("arrangement.getPlaylist", {"start_track": 3, "count": 32})
    assert [t["index"] for t in result["tracks"]] == [3, 4]
    assert result["clip_coverage"] == "not_exposed_by_midi_api"
    with pytest.raises(ValueError):
        module.dispatch("arrangement.getPlaylist", {"start_track": 0})
    assert host.calls == []


def test_audio_source_preflight_rejects_sampler_without_touching_ui(controller):
    module, host = controller
    with pytest.raises(ValueError, match="Audio Clip"):
        module.dispatch("arrangement.preparePlaylist", {"source_channel": 0})
    assert host.calls == []
    result = module.dispatch("arrangement.preparePlaylist", {"source_channel": 1})
    assert result["source"] == {
        "kind": "audio",
        "index": 1,
        "name": "Test audio",
        "name_occurrences": 1,
    }
    assert ("channel_select", 1, True) in host.calls
    assert ("tool", 0) in host.calls


def test_preparation_requires_pencil_api_and_rejects_two_sources(controller):
    module, host = controller
    with pytest.raises(ValueError, match="exactly one"):
        module.dispatch("arrangement.preparePlaylist", {"pattern_index": 2, "source_channel": 1})
    del module.playlist.selectTool
    with pytest.raises(RuntimeError, match="40"):
        module.dispatch("arrangement.preparePlaylist", {})
    assert host.calls == host.patterns.calls == []


def line(text, left, top, width=45, height=12):
    rect = (left, top, left + width, top + height)
    return {"text": text, "rect": rect, "words": [{"text": text, "rect": rect}]}


def ruler():
    return [line("1", 200, 50, 8), line("3", 400, 50, 8), line("5", 600, 50, 8)]


def tracks():
    return [{"index": i, "name": "Track " + str(i)} for i in range(1, 5)]


def test_calibration_uses_measured_ruler_and_variable_track_rows():
    records = ruler() + [
        line("Track 1", 20, 100),
        line("Track 2", 20, 150),
        line("Track 3", 20, 230),
    ]
    grid = native._calibrate(records, tracks(), (0, 0, 1000, 500))
    assert grid.pixels_per_bar == 100 and grid.canvas_left == 200
    assert grid.track_rows == {1: (97, 147), 2: (147, 227)}
    assert grid.point(2, 3) == (404, 187)
    with pytest.raises(RuntimeError, match="not visible"):
        grid.point(3, 1)  # Last readable row has no measured bottom.


def test_calibration_does_not_merge_rows_when_ocr_misses_intermediate_track():
    records = ruler() + [line("Track 1", 20, 100), line("Track 3", 20, 200)]
    with pytest.raises(RuntimeError, match="consecutive"):
        native._calibrate(records, tracks(), (0, 0, 1000, 500))


def test_calibration_rejects_duplicate_track_identity_and_distorted_ruler():
    duplicated = [{"index": i, "name": "Duplicate"} for i in (1, 2)]
    with pytest.raises(RuntimeError, match="consecutive"):
        native._calibrate(ruler() + [line("Duplicate", 20, 100)], duplicated, (0, 0, 1000, 500))
    with pytest.raises(RuntimeError, match="calibrate"):
        native._calibrate(
            [line("1", 200, 50), line("3", 400, 50), line("5", 900, 50)],
            tracks(),
            (0, 0, 1000, 500),
        )


def test_scrolled_grid_never_considers_picker_as_visible_canvas():
    grid = native.Grid((0, 0, 1000, 500), -200, 100, 56, {1: (100, 150)}, 200)
    assert grid.first_visible_bar() == 5
    assert grid.x(5) == 200
    with pytest.raises(RuntimeError, match="not visible"):
        grid.x(3)  # x=0 lies in Picker/header, though inside the whole FL window.


def test_locate_requires_exact_name_track_and_unambiguous_bar():
    grid = native.Grid((0, 0, 1000, 500), 200, 100, 56, {1: (100, 150)}, 200)
    target = {"name": "Bass", "track": 1, "rect": (204, 100, 240, 112)}
    assert native._locate([target], grid, "Bass", 1, 1) is target
    for clips, name, track in [
        ([target], "Bas", 1),
        ([target], "Bass", 2),
        ([target, dict(target)], "Bass", 1),
    ]:
        with pytest.raises(RuntimeError, match="exactly one"):
            native._locate(clips, grid, name, track, 1)


class Window:
    def __init__(self):
        self.calls = []
        self.root = 1
        self.keyboard = SimpleNamespace(pressed=lambda *unused: nullcontext())
        self.mouse = SimpleNamespace(position=(0, 0))

    def __enter__(self):
        self.calls.append(("enter",))
        return self

    def __exit__(self, *args):
        return None

    def click(self, point, **kwargs):
        self.calls.append(("click", point, kwargs))

    def press(self, key):
        self.calls.append(("press", key))

    def rect(self, hwnd):
        return (0, 0, 1000, 500)

    def require_foreground(self):
        pass

    def _check_point(self, hwnd, point):
        pass


@pytest.mark.parametrize("actual_start,actual_end", [(384, 768), (-1, -1), (0, 0), (0, 768)])
def test_clip_selection_requires_exact_nonempty_bounds(monkeypatch, actual_start, actual_end):
    monkeypatch.setitem(
        sys.modules, "pynput.keyboard", SimpleNamespace(Key=SimpleNamespace(ctrl=1))
    )
    monkeypatch.setattr(
        native,
        "_command",
        lambda *args: {
            "start_ticks": actual_start,
            "end_ticks": actual_end,
        },
    )
    monkeypatch.setattr(native, "clear_time_selection", lambda *args: {"ruler_point": (208, 56)})
    monkeypatch.setattr(native, "_playlist", lambda window: 2)
    monkeypatch.setattr(native, "_selection_cycle", lambda *args: {})
    window = Window()
    with pytest.raises(RuntimeError):
        native._select(window, None, {"rect": (204, 100, 240, 112)}, 0, 384)
    assert window.calls[0] == ("press", "ctrl+d")
    assert window.calls[-1] == ("press", "ctrl+enter")


def test_move_rejects_nonexistent_destination_before_any_native_action(monkeypatch):
    monkeypatch.setattr(native, "_command", lambda *args: {"tracks": tracks()})
    window = Window()
    monkeypatch.setattr(native, "FLWindows", lambda: window)
    with pytest.raises(ValueError, match="does not exist"):
        native.move_clip(None, "Bass", 1, 1, 5, 1)
    assert window.calls == []


def deletion_boundary(monkeypatch, remaining, tiles=None, changed_geometry=False):
    grid = native.Grid((0, 0, 1000, 500), 200, 100, 56, {1: (100, 150)}, 200)
    target = {"name": "Bass", "track": 1, "rect": (204, 100, 240, 112)}
    window = Window()
    monkeypatch.setattr(native, "FLWindows", lambda: window)
    monkeypatch.setattr(native, "_command", lambda *args: {"timing": {"ticks_per_bar": 384}})
    counts = iter(remaining)
    calls = []

    def view(*args):
        calls.append(1)
        clips = [target] if len(calls) <= 2 else [dict(target) for _ in range(next(counts))]
        current = grid
        if changed_geometry and len(calls) >= 3:
            current = native.Grid((0, 0, 999, 500), 200, 100, 56, {1: (100, 150)}, 200)
        return 1, current, clips, []

    def select(*args, witness_out=None):
        witness_out.update(
            selected_timeline="B",
            window_rect=(0, 0, 1000, 500),
            baseline="A",
            selected="B",
            region=(200, 96, 244, 116),
            neutral_point=(208, 56),
        )
        return {"start_ticks": 0, "end_ticks": 384}

    images = iter(["B", *(tiles or ["B", "C", "C", "C"])])
    monkeypatch.setattr(native, "_view", view)
    monkeypatch.setattr(native, "_select", select)
    monkeypatch.setattr(native, "clear_time_selection", lambda *args: {"ruler_point": (208, 56)})
    monkeypatch.setattr(native, "_move_pointer", lambda *args: None)
    monkeypatch.setattr(native, "_stable_tile", lambda *args: next(images))
    monkeypatch.setattr(native.time, "sleep", lambda seconds: None)
    return window


@pytest.mark.parametrize("remaining", [0, 1, 2])
def test_delete_requires_repeated_absence_not_ocr_ambiguity(monkeypatch, remaining):
    window = deletion_boundary(monkeypatch, [remaining] * 3)
    if remaining:
        with pytest.raises(RuntimeError, match="not verified|Multiple visible"):
            native.delete_clip(None, "Bass", 1, 1, True)
    else:
        result = native.delete_clip(None, "Bass", 1, 1, True)
        assert result["status"] == "deleted"
        assert result["verification"] == "three_label_absences_and_stable_changed_pixels"
    assert not any(call == ("press", "delete") for call in window.calls)
    assert window.calls[-1][2]["button"] == "right"


@pytest.mark.parametrize(
    "remaining,tiles,geometry",
    [
        ([0, 1, 0], ["B", "C"], False),  # OCR missing once before label returns.
        ([0, 0, 0], ["B", "B"], False),  # Pixels did not change.
        ([0, 0, 0], ["B", "A"], False),  # Only reverted to unselected appearance.
        ([0, 0, 0], ["B", "C", "D"], False),  # Pixels still changing.
        ([0, 0, 0], ["B"], True),  # View moved; previous crop is no longer evidence.
    ],
)
def test_delete_does_not_accept_transient_or_unrelated_visual_changes(
    monkeypatch,
    remaining,
    tiles,
    geometry,
):
    deletion_boundary(monkeypatch, remaining, tiles, geometry)
    with pytest.raises(RuntimeError):
        native.delete_clip(None, "Bass", 1, 1, True)


def test_clear_time_selection_needs_ruler_only_and_verifies_readback(monkeypatch):
    monkeypatch.setattr(native, "_playlist", lambda window: 2)
    monkeypatch.setattr(native, "_scan", lambda *args: ruler())
    monkeypatch.setattr(native, "_native_canvas_boundary", lambda window, hwnd, grid, records: grid)
    responses = iter([{"success": True}, {"start_ticks": -1, "end_ticks": -1}])
    monkeypatch.setattr(native, "_command", lambda *args: next(responses))
    window = Window()
    assert native.clear_time_selection(None, window)["status"] == "cleared"
    assert window.calls == [("click", (208, 56), {"count": 2})]
    responses = iter([{"success": True}, {"start_ticks": 0, "end_ticks": 384}])
    with pytest.raises(RuntimeError, match="not cleared"):
        native.clear_time_selection(None, window)


def test_native_confirmation_and_source_preflight_never_touch_fl(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("FL must not be contacted")

    monkeypatch.setattr(native, "_command", forbidden)
    with pytest.raises(ValueError, match="confirm"):
        native.delete_clip(None, "Bass", 1, 1, False)
    with pytest.raises(ValueError, match="confirm"):
        native.delete_pattern(None, 2, "Bass", False)
    with pytest.raises(ValueError, match="exactly one"):
        native.place_clip(None, 1, 1, pattern_index=2, source_channel=1)


class Registry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register


class Connection:
    def __init__(self, result=None, error=None):
        self.result = result or {"success": True}
        self.error = error
        self.calls = []
        self.locked = False

    def transaction(self):
        connection = self

        class Transaction:
            def __enter__(self):
                connection.locked = True

            def __exit__(self, *args):
                connection.locked = False

        return Transaction()

    def send_command(self, action, params, timeout):
        assert self.locked, "Whole workflow must own the mailbox transaction"
        self.calls.append((action, params))
        if self.error:
            raise self.error
        return self.result


def test_tools_are_registered_and_validate_coordinates_before_mailbox(monkeypatch):
    registry = Registry()
    tools.register_arrangement_tools(registry)
    assert len(registry.tools) == 18
    connection = Connection()
    monkeypatch.setattr(tools, "get_connection", lambda: connection)
    assert not registry.tools["fl_place_pattern_clip"](2, 0)["success"]
    assert not registry.tools["fl_place_audio_clip"](-1, 1)["success"]
    assert connection.calls == []
    connection.result = {"success": True, "quarter_bpm": 120.0}
    monkeypatch.setattr(
        tools,
        "_display_tempo",
        lambda conn: {
            "bpm": 120.0,
            "quarter_bpm": 120.0,
            "ui_to_quarter_ratio": 1.0,
            "tempo_unit": "fl_display_bpm",
        },
    )
    assert registry.tools["fl_set_tempo"](120)["success"]
    assert connection.calls == [
        ("arrangement.setTempo", {"display_bpm": 120.0, "quarter_bpm": 120.0})
    ]
    assert not connection.locked


@pytest.mark.parametrize("failure", ["timeout", "controller"])
def test_api_mutation_failures_are_unknown_and_not_retry_safe(monkeypatch, failure):
    connection = Connection(
        result={"success": False, "error": "length setter failed"}
        if failure == "controller"
        else None,
        error=RuntimeError("timeout after dispatch") if failure == "timeout" else None,
    )
    monkeypatch.setattr(tools, "get_connection", lambda: connection)
    result = tools._api("createPattern", {"name": "Lead"})
    assert not result["success"] and result["status"] == "unknown"
    assert result["inspect_project_before_retry"]
    assert len(connection.calls) == 1 and not connection.locked


def test_read_failure_is_failed_and_native_failure_is_unknown(monkeypatch):
    connection = Connection(error=RuntimeError("timeout"))
    monkeypatch.setattr(tools, "get_connection", lambda: connection)
    assert tools._api("getPatterns")["status"] == "failed"

    def mutate_then_fail(unused):
        raise RuntimeError("verification failed after insertion")

    result = tools._native(mutate_then_fail)
    assert result["status"] == "unknown" and result["inspect_project_before_retry"]
    assert not connection.locked


def test_active_default_pattern_can_be_selected_and_named(controller):
    module, host = controller
    result = module.dispatch("arrangement.selectPattern", {"index": 1})
    assert result["pattern"]["active"] and result["pattern"]["is_default"]
    result = module.dispatch("arrangement.renamePattern", {"index": 1, "name": "Intro"})
    assert result["pattern"]["name"] == "Intro"
    assert not result["pattern"]["is_default"]
    assert host.patterns.records[2]["notes"] == [36, 43]


@pytest.mark.parametrize("action", ["selectPattern", "renamePattern"])
def test_select_or_rename_does_not_implicitly_create_unallocated_default(controller, action):
    module, host = controller
    with pytest.raises(ValueError, match="does not contain"):
        module.dispatch("arrangement." + action, {"index": 3, "name": "Unallocated"})
    assert host.patterns.calls == []
    assert host.patterns.active == 1


@pytest.mark.parametrize("inverted", [False, True])
def test_tempo_detects_host_flag_and_uses_same_flag_for_setter(controller, inverted):
    module, host = controller
    host.inverted_tempo_mode = inverted
    assert module.dispatch("arrangement.getTiming", {})["quarter_bpm"] == 140.0
    result = module.dispatch("arrangement.setTempo", {"display_bpm": 122.5, "quarter_bpm": 122.5})
    assert result["quarter_bpm"] == 122.5 and host.bpm == 122.5
    assert host.calls == [("tempo", 122.5)]
    assert module._tempo_mode() == (122.5, inverted)


def test_tempo_prefers_false_mode_precision_when_both_results_are_valid(controller):
    module, host = controller
    host.bpm = 122.875
    assert module.mixer.getCurrentTempo(True) == 123
    assert module._tempo_mode() == (122.875, False)


def test_tempo_rejects_unrecognizable_units_without_calling_setter(controller):
    module, host = controller
    module.mixer.getCurrentTempo = lambda mode: math.nan
    with pytest.raises(RuntimeError, match="Neither tempo mode"):
        module.dispatch("arrangement.setTempo", {"display_bpm": 122.5, "quarter_bpm": 122.5})
    assert host.calls == []


def test_playlist_window_recognizes_current_event_form_without_accepting_piano_roll():
    window = SimpleNamespace(
        children=lambda class_name: [11, 12] if class_name == "TEventEditForm" else [],
        windows=lambda class_name: [11] if class_name == "TEventEditForm" else [],
        text=lambda hwnd: {11: "Playlist - Arrangement - MCP Pattern -", 12: "Piano roll - Drum"}[
            hwnd
        ],
    )
    assert native._playlist(window) == 11


def test_playlist_window_keeps_legacy_class_and_rejects_ambiguous_playlists():
    window = SimpleNamespace(
        children=lambda class_name: [10] if class_name == "TPlaylistForm" else [],
        windows=lambda class_name: [],
        text=lambda hwnd: "unused",
    )
    assert native._playlist(window) == 10
    window.windows = lambda class_name: [11] if class_name == "TEventEditForm" else []
    window.text = lambda hwnd: "Playlist - Second"
    with pytest.raises(RuntimeError, match="one unobstructed"):
        native._playlist(window)


def test_ocr_bounds_cap_portrait_window_without_expanding_explicit_regions():
    bounds = (-1812, -893, 0, 2793)
    assert native._scan_bounds(bounds) == (-1812, -893, 0, 207)
    assert native._scan_bounds(bounds, (-1000, -850, -100, -700)) == (-1000, -850, -100, -700)
    assert native._scan_bounds((0, 0, 800, 600)) == (0, 0, 800, 600)


def test_exact_header_readings_are_deduplicated_at_same_position():
    original = line("Track 1", 20, 100)
    enhanced = line("Track 1", 20.5, 100.5)
    other = line("Track 2", 20, 150)
    merged = native._merge_header_readings(
        [original],
        [enhanced, other],
        ["Track 1", "Track 2"],
        0,
        200,
        50,
    )
    assert merged == [original, other]


def test_conflicting_exact_header_identities_discard_row_instead_of_guessing():
    original = line("Track 1", 20, 100)
    conflicting = line("Track 2", 20, 101)
    later = line("Track 1", 20, 100.5)
    assert (
        native._merge_header_readings(
            [original],
            [conflicting, later],
            ["Track 1", "Track 2"],
            0,
            200,
            50,
        )
        == []
    )


def test_header_ocr_does_not_substitute_similar_letters_for_digits():
    partial = line("Track I", 20, 100)
    assert (
        native._merge_header_readings(
            [],
            [partial, line("Track S", 20, 150)],
            ["Track 1", "Track 5"],
            0,
            200,
            50,
        )
        == []
    )


def test_same_track_name_read_at_different_rows_remains_ambiguous():
    first = line("Track 1", 20, 100)
    second = line("Track 1", 20, 150)
    merged = native._merge_header_readings([first], [second], ["Track 1"], 0, 200, 50)
    assert merged == [first, second]


class ScrollerWindow:
    def __init__(self, scrollers=None, covered=False):
        self.bounds = (-1812, -893, 0, 2793)
        self.scrollers = (
            scrollers
            if scrollers is not None
            else {
                11010112: (-1540, -869, -20, -848),
            }
        )
        self.checks = []
        self.covered = covered

    def rect(self, hwnd):
        return self.bounds if hwnd == 6756710 else self.scrollers[hwnd]

    def children(self, parent=None, class_name=None):
        assert parent == 6756710 and class_name == "TQuickScroller"
        return list(self.scrollers)

    def _check_point(self, hwnd, point):
        self.checks.append((hwnd, point))
        if self.covered:
            raise RuntimeError("The expected FL control is covered")


def real_metadata_ruler(scrolled=False):
    origin = -1647.25 if scrolled else -1536.25
    labels = (
        [line(str(bar), origin + (bar - 1) * 55.5, -842, 4, 8) for bar in (3, 4, 5)]
        if scrolled
        else [line(str(bar), origin + (bar - 1) * 55.5, -842, 4, 8) for bar in (2, 3, 4)]
    )
    return native._calibrate(labels, [], (-1812, -893, 0, 207), require_tracks=False), labels


def test_real_scroller_metadata_makes_bar1_available_without_guessing_missing_label():
    grid, records = real_metadata_ruler()
    window = ScrollerWindow()
    with pytest.raises(RuntimeError, match="not visible"):
        grid.x(1)  # First OCR number is 2, despite bar 1 being in the canvas.
    assert native._native_canvas_boundary(window, 6756710, grid, records) is grid
    assert grid.x(1) == -1536.25
    assert grid.canvas_left == -1540
    assert grid.canvas_boundary_source == "playlist_horizontal_scroller"
    assert [hwnd for hwnd, point in window.checks] == [6756710, 11010112]


def test_scroller_boundary_still_rejects_bars_left_of_canvas_after_horizontal_scroll():
    grid, records = real_metadata_ruler(scrolled=True)
    native._native_canvas_boundary(ScrollerWindow(), 6756710, grid, records)
    assert grid.canvas_left == -1540
    assert grid.x(3) == -1536.25
    for bar in (1, 2):
        with pytest.raises(RuntimeError, match="not visible"):
            grid.x(bar)


@pytest.mark.parametrize(
    "scrollers",
    [
        {},
        {10: (-1540, -869, -20, -848), 11: (-1540, -868, -20, -847)},
        {10: (-1820, -869, -20, -848)},  # Extends outside this Playlist.
        {10: (-1540, -830, -20, -809)},  # Below the ruler.
        {10: (-1540, -892, -1520, -100)},  # Vertical scroller.
        {10: (-1540, -869, -1450, -848)},  # Cannot span three read ruler labels.
    ],
)
def test_unproven_scroller_geometry_keeps_conservative_boundary(scrollers):
    grid, records = real_metadata_ruler()
    old_left = grid.canvas_left
    window = ScrollerWindow(scrollers)
    native._native_canvas_boundary(window, 6756710, grid, records)
    assert grid.canvas_left == old_left
    assert grid.canvas_boundary_source == "first_readable_ruler_label"
    assert window.checks == []


def test_scroller_ownership_or_occlusion_failure_cannot_relax_canvas_boundary():
    grid, records = real_metadata_ruler()
    old_left = grid.canvas_left
    window = ScrollerWindow(covered=True)
    native._native_canvas_boundary(window, 6756710, grid, records)
    assert grid.canvas_left == old_left
    with pytest.raises(RuntimeError, match="not visible"):
        grid.x(1)


@pytest.mark.parametrize("clear_works,new_selection", [(True, False), (False, False), (True, True)])
def test_clip_selection_cannot_reuse_old_matching_timeline_range(
    monkeypatch,
    clear_works,
    new_selection,
):
    monkeypatch.setitem(
        sys.modules,
        "pynput.keyboard",
        SimpleNamespace(Key=SimpleNamespace(ctrl=1)),
    )
    monkeypatch.setattr(native, "_selection_cycle", lambda *args: {})
    monkeypatch.setattr(native, "_playlist", lambda window: 2)
    monkeypatch.setattr(native, "_scan", lambda *args: ruler())
    monkeypatch.setattr(native, "_native_canvas_boundary", lambda window, hwnd, grid, records: grid)

    class SelectionWindow(Window):
        def __init__(self):
            super().__init__()
            self.timeline = (0, 384)  # Stale range exactly matches the requested target.
            self.readbacks = []

        def click(self, point, **kwargs):
            super().click(point, **kwargs)
            if kwargs.get("count") == 2 and clear_works:
                self.timeline = (-1, -1)

        def press(self, key):
            super().press(key)
            if key == "ctrl+enter" and new_selection:
                self.timeline = (0, 384)

    window = SelectionWindow()

    def command(connection, action, params=None):
        if action == "arrangement.getSelection":
            window.readbacks.append(window.timeline)
            return {
                "success": True,
                "start_ticks": window.timeline[0],
                "end_ticks": window.timeline[1],
            }
        assert action == "arrangement.preparePlaylist"
        return {"success": True}

    monkeypatch.setattr(native, "_command", command)
    if clear_works and new_selection:
        result = native._select(window, None, {"rect": (204, 100, 240, 112)}, 0, 384)
        assert result["start_ticks"] == 0 and result["end_ticks"] == 384
        assert window.readbacks == [(-1, -1), (0, 384)]
    else:
        with pytest.raises(RuntimeError, match="not cleared|nonempty selected clip"):
            native._select(window, None, {"rect": (204, 100, 240, 112)}, 0, 384)
        if clear_works:
            assert window.readbacks == [(-1, -1), (-1, -1)]
        else:
            assert window.readbacks == [(0, 384)]
            assert ("press", "ctrl+enter") not in window.calls


@pytest.fixture(autouse=True)
def no_live_clip_captures(monkeypatch):
    target_records = native._target_records

    def forbidden(*args):
        raise AssertionError("Unit tests must supply a fake pixel capture")

    monkeypatch.setattr(native, "_capture_tile", forbidden)
    monkeypatch.setattr(native, "_target_records", lambda *args: [])
    monkeypatch.setattr(native, "_reset_clip_selection", lambda *args: None)
    return target_records


@pytest.mark.parametrize(
    "images,success",
    [
        (["A", "B", "A", "B"], True),
        (
            ["A", "A"],
            False,
        ),  # No selection change, including hover-only effects removed by neutral point.
        (["A", "B", "C"], False),  # Cannot reverse selection.
        (["A", "B", "A", "C"], False),  # Cannot reproduce selection.
    ],
)
def test_selection_requires_reversible_stable_clip_appearance(monkeypatch, images, success):
    monkeypatch.setitem(
        sys.modules, "pynput.keyboard", SimpleNamespace(Key=SimpleNamespace(ctrl=1, shift=2))
    )
    captures = iter(images)
    neutral_moves = []
    monkeypatch.setattr(native, "_stable_tile", lambda *args: next(captures))
    monkeypatch.setattr(
        native, "_move_pointer", lambda window, hwnd, point: neutral_moves.append(point)
    )
    window = Window()
    clip = {"rect": (204, 100, 240, 112)}
    if success:
        result = native._selection_cycle(window, 1, clip, (208, 56))
        assert result["baseline"] == "A" and result["selected"] == "B"
        assert len([call for call in window.calls if call[0] == "click"]) == 3
    else:
        with pytest.raises(RuntimeError, match="selection change"):
            native._selection_cycle(window, 1, clip, (208, 56))
    assert all(point == (208, 56) for point in neutral_moves)
    assert not any(call == ("press", "ctrl+enter") for call in window.calls)


def test_unselected_clip_cannot_use_ctrlenter_viewport_fallback(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "pynput.keyboard", SimpleNamespace(Key=SimpleNamespace(ctrl=1, shift=2))
    )
    monkeypatch.setattr(native, "clear_time_selection", lambda *args: {"ruler_point": (208, 56)})
    monkeypatch.setattr(native, "_playlist", lambda window: 1)
    monkeypatch.setattr(native, "_move_pointer", lambda *args: None)
    monkeypatch.setattr(native, "_stable_tile", lambda *args: "unchanged")
    monkeypatch.setattr(native, "_command", lambda *args: {"start_ticks": 0, "end_ticks": 384})
    window = Window()
    with pytest.raises(RuntimeError, match="independent selection change"):
        native._select(window, None, {"rect": (204, 100, 240, 112)}, 0, 384)
    assert ("press", "ctrl+enter") not in window.calls


@pytest.mark.parametrize("frames,passes", [(["A", "A", "A"], True), (["A", "B"] * 5, False)])
def test_tile_observation_requires_three_consecutive_identical_frames(monkeypatch, frames, passes):
    capture = iter(frames)
    monkeypatch.setattr(native, "_capture_tile", lambda *args: next(capture))
    monkeypatch.setattr(native.time, "sleep", lambda seconds: None)
    if passes:
        assert native._stable_tile(Window(), 1, (200, 96, 244, 116)) == "A"
    else:
        with pytest.raises(RuntimeError, match="not stable"):
            native._stable_tile(Window(), 1, (200, 96, 244, 116))


def test_drag_preserves_grab_offset_and_measured_track_tops():
    grid = native.Grid((-1000, -100, 0, 500), -800, 100, -44, {1: (0, 60), 2: (60, 140)}, -810)
    clip = {"rect": (-776, 5, -744, 17)}
    start, end = native._drag_points(grid, clip, 1, 1, 2, 3)
    assert start == (-760, 11) and end == (-560, 71)
    assert end[1] - grid.track_rows[2][0] == start[1] - grid.track_rows[1][0]
    assert end[0] - start[0] == 200  # Not simply snapped to bar 3's left edge.


@pytest.mark.parametrize(
    "track,bar,rect",
    [
        (3, 2, (224, 100, 260, 112)),  # Destination track offscreen.
        (1, 9, (224, 100, 260, 112)),  # Destination bar offscreen.
        (1, 8, (360, 100, 396, 112)),  # Actual grab offset pushes endpoint outside canvas.
    ],
)
def test_drag_preflight_rejects_any_invisible_endpoint(track, bar, rect):
    grid = native.Grid((0, 0, 1000, 500), 200, 100, 56, {1: (100, 150)}, 200)
    with pytest.raises((ValueError, RuntimeError)):
        native._drag_points(grid, {"rect": rect}, 1, 1, track, bar)


def test_drag_releases_left_button_when_foreground_or_motion_fails(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "pynput.mouse", SimpleNamespace(Button=SimpleNamespace(left="left"))
    )
    window = Window()
    mouse_calls = []
    window.mouse = SimpleNamespace(
        press=lambda button: mouse_calls.append(("down", button)),
        release=lambda button: mouse_calls.append(("up", button)),
    )
    moves = []

    def motion(*args):
        moves.append(args[-1])
        if len(moves) == 3:
            raise RuntimeError("Foreground changed")

    monkeypatch.setattr(native, "_move_pointer", motion)
    monkeypatch.setattr(native.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="Foreground changed"):
        native._drag_clip(window, 1, (224, 106), (424, 156))
    assert mouse_calls == [("down", "left"), ("up", "left")]


def test_move_revalidates_geometry_after_snap_without_pressing_mouse(monkeypatch):
    window = Window()
    monkeypatch.setattr(native, "FLWindows", lambda: window)
    monkeypatch.setattr(
        native, "_command", lambda *args: {"tracks": tracks(), "timing": {"ticks_per_bar": 384}}
    )
    target = {"name": "Bass", "track": 1, "rect": (204, 100, 240, 112)}
    first = native.Grid((0, 0, 1000, 500), 200, 100, 56, {1: (100, 150), 2: (150, 200)}, 200)
    second = native.Grid((0, 0, 990, 500), 200, 100, 56, {1: (100, 150), 2: (150, 200)}, 200)
    views = iter([(1, first, [target], []), (1, second, [target], [])])
    monkeypatch.setattr(native, "_view", lambda *args: next(views))
    monkeypatch.setattr(
        native, "_select", lambda *args, **kwargs: {"start_ticks": 0, "end_ticks": 384}
    )
    monkeypatch.setattr(native, "_snap_bar", lambda *args: None)

    def forbidden(*args):
        raise AssertionError("Drag must not start after geometry changes")

    monkeypatch.setattr(native, "_drag_clip", forbidden)
    with pytest.raises(RuntimeError, match="before drag"):
        native.move_clip(None, "Bass", 1, 1, 2, 3)
    assert not any(call[0] == "press" and call[1].startswith("shift+") for call in window.calls)


@pytest.mark.parametrize(
    "frame,success", [("S-with-range", True), ("A", False), ("changed", False)]
)
def test_drag_requires_fresh_selected_state_and_never_accepts_baseline(monkeypatch, frame, success):
    window = Window()
    monkeypatch.setattr(native, "_move_pointer", lambda *args: None)
    monkeypatch.setattr(native, "_stable_tile", lambda *args: frame)
    witness = {
        "baseline": "A",
        "selected": "S",
        "selected_timeline": "S-with-range",
        "region": (200, 96, 244, 116),
        "neutral_point": (208, 56),
        "window_rect": (0, 0, 1000, 500),
    }
    if success:
        native._assert_selected_source(window, 1, witness)
    else:
        with pytest.raises(RuntimeError, match="freshly witnessed selected state"):
            native._assert_selected_source(window, 1, witness)
    assert window.calls == []


def test_windows_api45_raw_length4_is_one_quarter_note_beat(controller):
    module, host = controller
    result = module.dispatch("arrangement.getPattern", {"index": 2})["pattern"]
    assert result["length_steps"] == 4
    assert result["length_beats"] == 1.0
    assert result["length_raw_unit"] == "sixteenth_note_steps"
    assert not result["length_unit_assumed"]
    assert result["length_beats"] * module._timing()["ppq"] == 96


@pytest.mark.parametrize("beats,steps", [(4, 16), (0.25, 1), (1.5, 6)])
def test_create_converts_quarter_beats_to_real_host_steps(controller, beats, steps):
    module, host = controller
    result = module.dispatch("arrangement.createPattern", {"name": "Units", "length_beats": beats})
    pattern = result["pattern"]
    assert ("length", pattern["index"], steps) in host.patterns.calls
    assert pattern["length_steps"] == steps and pattern["length_beats"] == beats
    assert pattern["length_beats"] * module._timing()["ppq"] == beats * 96


@pytest.mark.parametrize("beats", [0.1, 0.3, math.nan, math.inf, 16384.25, True, "4"])
def test_invalid_fractional_pattern_length_is_rejected_before_edits(controller, beats):
    module, host = controller
    with pytest.raises(ValueError):
        module.dispatch("arrangement.createPattern", {"name": "Units", "length_beats": beats})
    assert host.patterns.calls == []


def test_clone_compares_raw_steps_and_reports_converted_beats(controller):
    module, host = controller
    host.patterns.records[2]["length"] = 16
    result = module.dispatch("arrangement.clonePattern", {"index": 2, "destination": 4})
    assert result["pattern"]["length_steps"] == 16
    assert result["pattern"]["length_beats"] == 4.0
    assert host.patterns.records[4]["length"] == 16


def test_length_conversion_discloses_assumption_for_other_api_versions(controller):
    module, host = controller
    host.version = 44
    result = module.dispatch("arrangement.getPattern", {"index": 2})["pattern"]
    assert result["length_beats"] == 1 and result["length_unit_assumed"]


@pytest.mark.parametrize("steps,end_ticks", [(4, 96), (16, 384)])
def test_controller_source_units_reach_native_clip_tick_verification(
    controller,
    monkeypatch,
    steps,
    end_ticks,
):
    module, host = controller
    monkeypatch.setattr(native, "_sidebar_source", lambda *args: None)
    host.patterns.records[2]["length"] = steps
    monkeypatch.setattr(
        native, "_command", lambda conn, action, params=None: module.dispatch(action, params or {})
    )
    monkeypatch.setattr(native, "FLWindows", lambda: Window())
    monkeypatch.setattr(native, "_snap_bar", lambda *args: None)
    monkeypatch.setattr(native.time, "sleep", lambda seconds: None)
    grid = native.Grid((0, 0, 1000, 500), 200, 100, 56, {1: (100, 150)}, 200)
    clip = {"name": "Bass", "track": 1, "rect": (204, 100, 240, 112)}
    views = iter([(1, grid, [], []), (1, grid, [], []), (1, grid, [clip], [])])
    monkeypatch.setattr(native, "_view", lambda *args: next(views))

    def selected(window, conn, clip, start, end):
        assert start == 0 and end == end_ticks
        return {"start_ticks": start, "end_ticks": end_ticks}

    monkeypatch.setattr(native, "_select", selected)
    result = native.place_clip(None, 1, 1, pattern_index=2)
    assert result["source"]["length_steps"] == steps
    assert result["source"]["length_beats"] == steps / 4
    assert result["end_ticks"] == end_ticks


def test_real_pattern_confirmation_ok_ocr_requires_native_button_ownership(monkeypatch):
    window = Window()
    window.windows = lambda: [1, 2]
    window.class_name = lambda hwnd: {
        1: "TFruityLoopsMainForm",
        2: "TMsgForm",
        3: "TQuickFocusBtn",
    }[hwnd]
    window.children = lambda hwnd: [3]
    window.rect = lambda hwnd: (100, 100, 180, 130) if hwnd == 3 else (0, 0, 300, 200)
    monkeypatch.setattr(
        native,
        "_scan",
        lambda *args: [
            line("The selected pattern is about to be deleted.", 10, 30),
            line("0k", 120, 108, 15, 10),
            line("Cancel", 210, 108),
        ],
    )
    native._confirm_pattern_delete(window)
    assert window.calls == [("click", (127.5, 113.0), {})]
    window.children = lambda hwnd: []
    with pytest.raises(RuntimeError, match="unique native button"):
        native._confirm_pattern_delete(window)


def test_six_eight_verifies_three_quarter_beats_without_claiming_independent_numerator(controller):
    module, host = controller
    result = module.dispatch("arrangement.setTimeSignature", {"numerator": 6, "denominator": 8})
    assert result["success"] and result["ticks_per_bar"] == 288
    assert result["quarter_beats_per_bar"] == 3
    assert result["numerator"] is None and result["denominator"] is None
    assert result["numerator_requested"] == 6 and result["denominator_requested"] == 8
    assert result["verification"] == "bar_tick_ratio_readback; no independent signature getters"
    restored = module.dispatch("arrangement.setTimeSignature", {"numerator": 4, "denominator": 4})
    assert restored["ticks_per_bar"] == 384 and restored["quarter_beats_per_bar"] == 4


def test_performance_mode_is_reported_and_refused_before_ordinary_clip_edits(controller):
    module, host = controller
    module.playlist.getPerformanceModeState = lambda: True
    assert module.dispatch("arrangement.getPlaylist", {"count": 1})["performance_mode"]
    with pytest.raises(RuntimeError, match="Performance mode"):
        module.dispatch("arrangement.preparePlaylist", {"track": 1, "pattern_index": 2})
    assert host.calls == [] and host.patterns.calls == []


def test_targeted_clip_ocr_accepts_only_small_source_icon_before_exact_name():
    icon = {"text": "3", "rect": (201, 101, 210, 111)}
    name = {"text": "Pat4", "rect": (214, 101, 238, 111)}
    assert native._target_name_records([{"words": [icon, name]}], "Pat4", 200) == [
        (214, 101, 238, 111)
    ]
    prefix = {"text": "Other", "rect": (214, 101, 242, 111)}
    assert native._target_name_records([{"words": [prefix, name]}], "Pat4", 200) == []
    real_prefix = {"text": "3", "rect": (214, 101, 220, 111)}
    assert native._target_name_records([{"words": [real_prefix, name]}], "Pat4", 200) == []


def test_targeted_clip_ocr_recovers_small_title_without_inventing_name(monkeypatch):
    grid = native.Grid((0, 0, 1000, 500), 200, 100, 56, {1: (100, 150)}, 200)
    monkeypatch.setattr(native, "_target_records", lambda *args: [line("Pat4", 214, 105)])
    assert native._target_clip(Window(), 1, grid, [], "Pat4", 1, 1)["name"] == "Pat4"
    assert native._target_clip(Window(), 1, grid, [], "Other", 1, 1, required=False) is None


@pytest.mark.parametrize("quarter,displayed,denominator", [(5.0, 10.0, 8), (2088.0, 522.0, 1)])
def test_quarter_tempo_outside_display_range_remains_readable_and_writable(
    controller,
    quarter,
    displayed,
    denominator,
):
    module, host = controller
    host.inverted_tempo_mode = True
    host.denominator = denominator
    host.bpm = displayed
    assert module._tempo_mode() == (quarter, True)
    result = module.dispatch(
        "arrangement.setTempo", {"display_bpm": displayed, "quarter_bpm": quarter}
    )
    assert result["quarter_bpm"] == quarter and result["tempo_unit"] == "quarter_note_bpm"
    assert host.calls == [("tempo", displayed)]


def test_view_comparison_tolerates_observed_ocr_subpixels_but_not_native_layout_changes():
    window = Window()
    first = native.Grid((0, 0, 1000, 500), 200, 111, 56, {1: (100, 150)}, 200)
    jitter = native.Grid((0, 0, 1000, 500), 200.75, 112, 56.125, {1: (100.25, 150.5)}, 200)
    assert native._same_view(
        native._view_signature(window, 1, first), native._view_signature(window, 1, jitter)
    )
    jitter.canvas_left = 210
    assert not native._same_view(
        native._view_signature(window, 1, first), native._view_signature(window, 1, jitter)
    )


def test_move_opens_snap_before_selected_witness_and_never_clears_between_witness_and_drag(
    monkeypatch,
):
    window = Window()
    order = []
    monkeypatch.setattr(native, "FLWindows", lambda: window)
    monkeypatch.setattr(
        native, "_command", lambda *args: {"tracks": tracks(), "timing": {"ticks_per_bar": 384}}
    )
    grid = native.Grid((0, 0, 1000, 500), 200, 100, 56, {1: (100, 150), 2: (150, 200)}, 200)
    source = {"name": "Bass", "track": 1, "rect": (204, 100, 240, 112)}
    target = {"name": "Bass", "track": 2, "rect": (404, 150, 440, 162)}
    views = iter([(1, grid, [source], []), (1, grid, [source], []), (1, grid, [target], [])])
    monkeypatch.setattr(native, "_view", lambda *args: next(views))
    monkeypatch.setattr(native, "_reset_clip_selection", lambda *args: order.append("reset"))
    monkeypatch.setattr(native, "_snap_bar", lambda *args: order.append("snap"))

    def selected(window, connection, clip, start, end=None, witness_out=None):
        order.append("select")
        if witness_out is not None:
            witness_out.update(selected_timeline="S-with-range")
        return {"start_ticks": start, "end_ticks": start + 384}

    monkeypatch.setattr(native, "_select", selected)
    monkeypatch.setattr(native, "_assert_selected_source", lambda *args: order.append("guard-S"))
    monkeypatch.setattr(native, "_drag_clip", lambda *args: order.append("drag"))
    result = native.move_clip(None, "Bass", 1, 1, 2, 3)
    assert result["status"] == "moved"
    assert order == ["reset", "snap", "select", "guard-S", "drag", "reset", "select"]


def test_six_eight_native_setter_uses_displayed160_and_expects_quarter80(controller):
    module, host = controller
    host.inverted_tempo_mode = True
    host.denominator = 8
    assert module._tempo_mode() == (70.0, True)
    result = module.dispatch("arrangement.setTempo", {"display_bpm": 160, "quarter_bpm": 80})
    assert result["quarter_bpm"] == 80
    assert result["display_bpm_written"] == 160
    assert host.bpm == 160 and host.calls == [("tempo", 160.0)]


@pytest.mark.parametrize("params", [{"quarter_bpm": 80}, {"display_bpm": 160}, {"bpm": 140}])
def test_tempo_write_rejects_ambiguous_single_unit_before_native_mutation(controller, params):
    module, host = controller
    with pytest.raises(ValueError, match="requires explicit"):
        module.dispatch("arrangement.setTempo", params)
    assert host.calls == [] and host.bpm == 140


@pytest.mark.parametrize("quarter", [0, -1, 2088.01, math.nan, math.inf, True, "80"])
def test_tempo_write_prevalidates_expected_quarter_value(controller, quarter):
    module, host = controller
    with pytest.raises(ValueError):
        module.dispatch("arrangement.setTempo", {"display_bpm": 160, "quarter_bpm": quarter})
    assert host.calls == [] and host.bpm == 140


def test_tempo_wrong_quarter_expectation_is_detected_after_one_native_write(controller):
    module, host = controller
    host.inverted_tempo_mode = True
    host.denominator = 8
    with pytest.raises(RuntimeError, match="readback"):
        module.dispatch("arrangement.setTempo", {"display_bpm": 160, "quarter_bpm": 90})
    assert host.calls == [("tempo", 160.0)] and host.bpm == 160


def test_drag_top_offset_matches_observed_unequal_row_geometry():
    grid = native.Grid(
        (411, 82, 2560, 1182), 685, 112, 137.5, {2: (194.75, 242.75), 3: (242.75, 283)}, 683
    )
    clip = {"rect": (697.85, 208.5, 720.08, 218.4)}
    start, end = native._drag_points(grid, clip, 2, 1, 3, 2)
    assert end[0] - start[0] == pytest.approx(112)
    # Center delta would be44 and could retain the old row.
    assert end[1] - start[1] == pytest.approx(48)
    assert end[1] - 242.75 == pytest.approx(start[1] - 194.75)


def test_drag_top_offset_handles_negative_vertical_coordinates():
    grid = native.Grid((-1000, -200, 0, 500), -800, 100, -144, {1: (-100, -40), 2: (-40, 0)}, -810)
    start, end = native._drag_points(grid, {"rect": (-776, -88, -744, -76)}, 1, 1, 2, 2)
    assert start == (-760, -82) and end == (-660, -22)
    assert end[1] - (-40) == start[1] - (-100)


def test_drag_top_offset_rejects_grab_offset_beyond_short_destination_row():
    grid = native.Grid((0, 0, 1000, 500), 200, 100, 56, {1: (100, 180), 2: (180, 200)}, 200)
    with pytest.raises(ValueError, match="grab points"):
        native._drag_points(grid, {"rect": (214, 165, 240, 175)}, 1, 1, 2, 2)


@pytest.mark.parametrize(
    "hints,success",
    [
        (["Picker: audio clips"] * 2, True),
        (["Picker: patterns"] * 2, False),
        (["Picker: audio clips", "Picker: patterns"], False),
    ],
)
def test_picker_tab_requires_two_real_matching_hints_before_click(monkeypatch, hints, success):
    window = Window()
    geometry = ((411, 82, 2560, 1392), (413, 83, 2558, 106), (551, 161, 567, 1392))
    monkeypatch.setattr(native, "_picker_geometry", lambda *args: geometry)
    monkeypatch.setattr(native, "_move_pointer", lambda *args: None)
    monkeypatch.setattr(native.time, "sleep", lambda *args: None)
    iterator = iter(hints)
    monkeypatch.setattr(native, "_command", lambda *args: {"hint": next(iterator)})
    if success:
        assert native._set_picker_tab(window, None, 1, "audio") == geometry[2]
        assert window.calls == [("click", (481.0, 133.5), {})]
    else:
        with pytest.raises(RuntimeError, match="hint did not match twice"):
            native._set_picker_tab(window, None, 1, "audio")
        assert window.calls == []


def test_pattern_source_clicks_exact_sidebar_word_then_checks_active_api_identity(monkeypatch):
    window = Window()
    window.rect = lambda hwnd: (411, 82, 2560, 1392)
    monkeypatch.setattr(native, "_playlist", lambda *args: 1)
    monkeypatch.setattr(native, "_set_picker_tab", lambda *args: (551, 161, 567, 1392))
    monkeypatch.setattr(
        native,
        "_picker_geometry",
        lambda *args: ((411, 82, 2560, 1392), (413, 83, 2558, 106), (551, 161, 567, 1392)),
    )
    icon = {"text": "a", "rect": (414.25, 180, 426.25, 190)}
    name = {"text": "Beat", "rect": (429.75, 180, 453.75, 190)}
    monkeypatch.setattr(native, "_scan", lambda *args: [{"text": "a Beat", "words": [icon, name]}])

    def active_after_click(*args):
        assert window.calls[-1] == ("click", (441.75, 185.0), {})
        return {"pattern": {"active": True, "name": "Beat"}}

    monkeypatch.setattr(native, "_command", active_after_click)
    native._sidebar_source(window, None, {"kind": "pattern", "index": 1, "name": "Beat"})
    assert window.calls == [("click", (441.75, 185.0), {})]
    monkeypatch.setattr(
        native, "_command", lambda *args: {"pattern": {"active": False, "name": "Beat"}}
    )
    with pytest.raises(RuntimeError, match="requested source pattern"):
        native._sidebar_source(window, None, {"kind": "pattern", "index": 1, "name": "Beat"})


@pytest.mark.parametrize(
    "problem", ["real_prefix", "two_letters", "suffix", "outside", "duplicate"]
)
def test_pattern_picker_does_not_strip_real_name_words_or_accept_ambiguous_rows(
    monkeypatch, problem
):
    window = Window()
    window.rect = lambda hwnd: (411, 82, 2560, 1392)
    monkeypatch.setattr(native, "_playlist", lambda *args: 1)
    monkeypatch.setattr(native, "_set_picker_tab", lambda *args: (551, 161, 567, 1392))
    monkeypatch.setattr(
        native,
        "_picker_geometry",
        lambda *args: ((411, 82, 2560, 1392), (413, 83, 2558, 106), (551, 161, 567, 1392)),
    )
    icon = {"text": "a", "rect": (414.25, 180, 426.25, 190)}
    name = {"text": "Beat", "rect": (429.75, 180, 453.75, 190)}
    words = [icon, name]
    if problem == "real_prefix":
        icon["rect"] = (428, 180, 433, 190)
    elif problem == "two_letters":
        icon["text"] = "ab"
    elif problem == "suffix":
        words.append({"text": "Extra", "rect": (460, 180, 490, 190)})
    elif problem == "outside":
        icon["rect"] = (400, 180, 410, 190)
    records = [{"text": "a Beat", "words": words}]
    if problem == "duplicate":
        records *= 2
    monkeypatch.setattr(native, "_scan", lambda *args: records)
    monkeypatch.setattr(native, "_command", lambda *args: pytest.fail("Must not accept API alone"))
    with pytest.raises(RuntimeError, match="exactly one fully readable source"):
        native._sidebar_source(window, None, {"kind": "pattern", "index": 1, "name": "Beat"})
    assert window.calls == []


def test_picker_native_geometry_accepts_only_owned_sidebar_shaped_bar():
    window = Window()
    rectangles = {
        1: (411, 82, 2560, 1392),
        2: (413, 83, 2558, 106),
        3: (551, 161, 567, 1392),
        4: (2300, 150, 2316, 1392),
    }
    window.rect = rectangles.__getitem__
    window.text = lambda hwnd: "Playlist -" if hwnd == 2 else ""
    window.children = lambda parent=None, class_name=None: (
        [2] if class_name == "TNewCaption" else [3, 4]
    )
    assert native._picker_geometry(window, 1) == (rectangles[1], rectangles[2], rectangles[3])
    rectangles[4] = (560, 161, 576, 1392)
    with pytest.raises(RuntimeError, match="one visible"):
        native._picker_geometry(window, 1)


def test_picker_geometry_change_after_hints_prevents_click(monkeypatch):
    window = Window()
    first = ((411, 82, 2560, 1392), (413, 83, 2558, 106), (551, 161, 567, 1392))
    changed = ((411, 82, 2560, 1392), (413, 83, 2558, 106), (561, 161, 577, 1392))
    geometries = iter([first, changed])
    monkeypatch.setattr(native, "_picker_geometry", lambda *args: next(geometries))
    monkeypatch.setattr(native, "_move_pointer", lambda *args: None)
    monkeypatch.setattr(native.time, "sleep", lambda *args: None)
    monkeypatch.setattr(native, "_command", lambda *args: {"hint": "Picker: audio clips"})
    with pytest.raises(RuntimeError, match="geometry changed"):
        native._set_picker_tab(window, None, 1, "audio")
    assert window.calls == []


@pytest.mark.parametrize(
    "labels,success", [(["Audio"], True), (["MCP Audio"], False), (["Audio", "Audio"], False)]
)
def test_audio_picker_source_requires_one_exact_name_inside_sidebar(monkeypatch, labels, success):
    window = Window()
    window.rect = lambda hwnd: (411, 82, 2560, 1392)
    monkeypatch.setattr(native, "_playlist", lambda *args: 1)
    monkeypatch.setattr(native, "_set_picker_tab", lambda *args: (551, 161, 567, 1392))
    monkeypatch.setattr(
        native,
        "_picker_geometry",
        lambda *args: ((411, 82, 2560, 1392), (413, 83, 2558, 106), (551, 161, 567, 1392)),
    )

    def scan(window, hwnd, region):
        assert region == (411, 145, 551, 1182)
        return [line(label, 430, 180 + i * 30) for i, label in enumerate(labels)]

    monkeypatch.setattr(native, "_scan", scan)
    if success:
        native._sidebar_source(window, None, {"kind": "audio", "index": 5, "name": "Audio"})
        assert window.calls == [("click", (452.5, 186.0), {})]
    else:
        with pytest.raises(RuntimeError, match="exactly one fully readable source"):
            native._sidebar_source(window, None, {"kind": "audio", "index": 5, "name": "Audio"})
        assert window.calls == []


def ruler_recovery_window(width=2560):
    window = Window()
    rectangles = {1: (411, 82, width, 1392), 2: (683, 106, width - 20, 127)}
    window.rect = rectangles.__getitem__
    window.class_name = lambda hwnd: "TEventEditForm" if hwnd == 1 else "TQuickScroller"
    window.text = lambda hwnd: "Playlist - Arrangement"
    window.children = lambda parent=None, class_name=None: [2]
    return window, rectangles


def test_scan_only_recovers_a_failed_playlist_ruler(monkeypatch):
    from PIL import Image, ImageGrab

    window, rectangles = ruler_recovery_window()
    picture = Image.new("RGB", (2149, 1100))
    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: picture)
    labels = [
        line("12", 1851, 133, 8, 8),
        line("13", 1961, 133, 10, 8),
        line("15", 2181, 133, 8, 8),
    ]
    calls = []

    def read(picture, bounds, contrast=1, pre_scale=1):
        calls.append((bounds, contrast, pre_scale))
        if len(calls) == 1:
            return [line("Audio", 800, 350)]
        assert contrast == pre_scale == 2
        return [item for item in labels if bounds[0] <= item["rect"][0] < bounds[2]]

    monkeypatch.setattr(native, "_picture_records", read)
    monkeypatch.setattr(
        native, "_enhance_headers", lambda picture, bounds, records, tracks: records
    )
    records = native._scan(window, 1, tracks=tracks())
    assert len(calls) == 10  # One full scan plus nine bounded, overlapping strips.
    assert [record["text"] for record in records] == ["Audio", "12", "13", "15"]
    grid = native._calibrate(records, [], (411, 82, 2560, 1182), require_tracks=False)
    assert grid.pixels_per_bar == 110 and grid.ruler_y == 137
    assert native._native_canvas_boundary(window, 1, grid, records).canvas_left == 683
    assert grid.x(2) == 751

    # Successful initial calibration does no recovery OCR, with or without tracks.
    calls.clear()
    monkeypatch.setattr(native, "_picture_records", lambda *args: labels)
    monkeypatch.setattr(native, "_recover_ruler", lambda *args: pytest.fail("Unexpected recovery"))
    assert native._scan(window, 1, tracks=tracks()) == labels
    assert native._scan(window, 1) == labels


def test_ruler_merge_replaces_only_band_numbers_and_deduplicates_overlaps():
    strip = (683, 127, 2540, 151)
    preserved = [line("Audio", 800, 133), line("140", 380, 10), line("12", 800, 300)]
    old = line("99", 750, 133)
    twelve = line("12", 750, 133, 10, 8)
    duplicate = line("12", 750.25, 133.25, 10, 8)
    result = native._merge_ruler_readings([old, *preserved], [twelve, duplicate], strip)
    assert result == [*preserved, twelve]
    # A cropped single '1' and complete '12' at the same position conflict.
    conflict = native._merge_ruler_readings(
        [], [line("1", 750, 133, 4, 8), twelve, duplicate], strip
    )
    assert conflict == []


@pytest.mark.parametrize("reason", ["ambiguous", "outside", "covered", "piano_roll"])
def test_ruler_native_recovery_refuses_unproven_geometry(monkeypatch, reason):
    window, rectangles = ruler_recovery_window()
    if reason == "ambiguous":
        rectangles[3] = rectangles[2]
        window.children = lambda **kwargs: [2, 3]
    elif reason == "outside":
        rectangles[2] = (683, 106, 2700, 127)
    elif reason == "covered":
        window._check_point = lambda *args: (_ for _ in ()).throw(RuntimeError("Covered"))
    else:
        window.text = lambda hwnd: "Piano roll - Sampler"
    monkeypatch.setattr(
        native, "_picture_records", lambda *args, **kwargs: pytest.fail("Unexpected OCR")
    )
    with pytest.raises(RuntimeError):
        native._recover_ruler(window, 1, None, (411, 82, 2560, 1182), [])


def test_ruler_recovery_refuses_budget_before_any_ocr(monkeypatch):
    window, rectangles = ruler_recovery_window(width=10000)
    monkeypatch.setattr(
        native, "_picture_records", lambda *args, **kwargs: pytest.fail("Unexpected OCR")
    )
    with pytest.raises(RuntimeError, match="budget"):
        native._recover_ruler(window, 1, None, (411, 82, 10000, 1182), [])


def test_ruler_recovery_rejects_changed_geometry_between_strips(monkeypatch):
    from PIL import Image

    window, rectangles = ruler_recovery_window()
    count = []

    def read(*args, **kwargs):
        count.append(1)
        rectangles[2] = (684, 106, 2540, 127)
        return []

    monkeypatch.setattr(native, "_picture_records", read)
    with pytest.raises(RuntimeError, match="geometry changed"):
        native._recover_ruler(window, 1, Image.new("RGB", (2149, 1100)), (411, 82, 2560, 1182), [])
    assert len(count) == 1


def test_ruler_recovery_cannot_keep_old_numbers_or_choose_another_numeric_band(monkeypatch):
    from PIL import Image

    window, rectangles = ruler_recovery_window()
    bounds = (411, 82, 2560, 1182)
    picture = Image.new("RGB", (2149, 1100))
    monkeypatch.setattr(native, "_picture_records", lambda *args, **kwargs: [])
    old = [line(str(i), 751 + (i - 2) * 110, 133, 8, 8) for i in (2, 3, 4)]
    with pytest.raises(RuntimeError, match="calibrate"):
        native._recover_ruler(window, 1, picture, bounds, old)
    outside = [line(str(i), 751 + (i - 2) * 110, 300, 8, 8) for i in (2, 3, 4)]
    with pytest.raises(RuntimeError, match="native Playlist ruler band"):
        native._recover_ruler(window, 1, picture, bounds, outside)


def test_clear_time_selection_full_playlist_scan_recovers_ruler_without_tracks(monkeypatch):
    from PIL import Image, ImageGrab

    window, rectangles = ruler_recovery_window()
    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: Image.new("RGB", (2149, 1100)))
    labels = [line(str(i), 751 + (i - 2) * 110, 133, 8, 8) for i in (12, 13, 15)]
    calls = []

    def read(picture, bounds, contrast=1, pre_scale=1):
        calls.append(bounds)
        return (
            []
            if len(calls) == 1
            else [label for label in labels if bounds[0] <= label["rect"][0] < bounds[2]]
        )

    monkeypatch.setattr(native, "_picture_records", read)
    monkeypatch.setattr(native, "_playlist", lambda window: 1)
    monkeypatch.setattr(
        native, "_enhance_headers", lambda *args: pytest.fail("No track scan requested")
    )
    monkeypatch.setattr(
        native, "_command", lambda *args: {"success": True, "start_ticks": 0, "end_ticks": -1}
    )
    result = native.clear_time_selection(None, window)
    assert result["status"] == "cleared"
    assert len(calls) == 10
    assert window.calls == [("click", (759, 137), {"count": 2})]


@pytest.mark.parametrize(
    "class_name,title,region",
    [
        ("TQuickPopupMenuWindow", "", None),
        ("TPanel", "Tempo", None),
        ("TEventEditForm", "Piano roll - Sampler", None),
        ("TEventEditForm", "Playlist - Arrangement", (683, 127, 939, 151)),
    ],
)
def test_menu_toolbar_and_local_roi_scans_never_attempt_ruler_recovery(
    monkeypatch, class_name, title, region
):
    from PIL import Image, ImageGrab

    window, rectangles = ruler_recovery_window()
    window.class_name = lambda hwnd: class_name
    window.text = lambda hwnd: title
    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: Image.new("RGB", (256, 24)))
    monkeypatch.setattr(native, "_picture_records", lambda *args: [])
    monkeypatch.setattr(
        native, "_recover_ruler", lambda *args: pytest.fail("Unexpected ruler recovery")
    )
    assert native._scan(window, 1, region=region) == []


@pytest.mark.parametrize("new_overlap", [False, True])
def test_placement_recalibrates_point_and_rechecks_overlap_after_snap(monkeypatch, new_overlap):
    window = Window()
    monkeypatch.setattr(native, "FLWindows", lambda: window)
    prepared = {
        "source": {"kind": "pattern", "name": "Beat", "index": 1, "length_beats": 4},
        "timing": {"ticks_per_bar": 384, "ppq": 96},
    }
    monkeypatch.setattr(native, "_command", lambda *args: prepared)
    monkeypatch.setattr(native, "_sidebar_source", lambda *args: None)
    monkeypatch.setattr(native, "_snap_bar", lambda *args: None)
    monkeypatch.setattr(native.time, "sleep", lambda *args: None)
    first = native.Grid((0, 0, 1000, 500), 200, 100, 56, {1: (100, 150)}, 200)
    after = native.Grid((0, 0, 1000, 500), 260, 120, 56, {1: (100, 150)}, 260)
    clip = {"name": "Beat", "track": 1, "rect": (264, 100, 300, 112), "approximate_bar": 1}
    post_menu_clips = [clip] if new_overlap else []
    views = iter([(1, first, [], []), (1, after, post_menu_clips, []), (1, after, [clip], [])])
    monkeypatch.setattr(native, "_view", lambda *args: next(views))
    monkeypatch.setattr(native, "_select", lambda *args: {"start_ticks": 0, "end_ticks": 384})
    if new_overlap:
        with pytest.raises(RuntimeError, match="overlap was not authorized"):
            native.place_clip(None, 1, 1, pattern_index=1)
        assert not any(call[0] == "click" for call in window.calls)
    else:
        assert native.place_clip(None, 1, 1, pattern_index=1)["success"]
        assert [call for call in window.calls if call[0] == "click"] == [("click", (264, 125), {})]


def test_picker_list_roi_includes_complete_first_row_above_sidebar_scroller(monkeypatch):
    window = Window()
    geometry = ((411, 82, 2560, 1392), (413, 83, 2558, 106), (551, 161, 567, 1392))
    monkeypatch.setattr(native, "_playlist", lambda *args: 1)
    monkeypatch.setattr(native, "_set_picker_tab", lambda *args: geometry[2])
    monkeypatch.setattr(native, "_picker_geometry", lambda *args: geometry)
    icon = {"text": "a", "rect": (414.25, 157.75, 426.25, 168.25)}
    name = {"text": "Beat", "rect": (429.75, 157.75, 453.75, 168.25)}

    def scan(window, hwnd, region):
        assert region == (411, 145, 551, 1182)
        assert geometry[1][3] <= region[1] <= name["rect"][1] < geometry[2][1]
        return [{"text": "a Beat", "words": [icon, name]}]

    monkeypatch.setattr(native, "_scan", scan)
    monkeypatch.setattr(
        native, "_command", lambda *args: {"pattern": {"active": True, "name": "Beat"}}
    )
    native._sidebar_source(window, None, {"kind": "pattern", "index": 1, "name": "Beat"})
    assert window.calls == [("click", (441.75, 163.0), {})]


@pytest.mark.parametrize(
    "bad_rect",
    [(199, 110, 220, 120), (290, 110, 301, 120), (220, 110, 220, 120), (math.nan, 110, 230, 120)],
)
def test_target_ocr_discards_flat_empty_and_out_of_bounds_word_boxes(
    monkeypatch, bad_rect, no_live_clip_captures
):
    from PIL import Image, ImageGrab

    picture = Image.new("RGB", (100, 50), (51, 51, 51))
    picture.putpixel((11, 11), (51, 51, 52))  # Even a one-level color change survives.
    good = line("Beat", 210, 110, 20, 10)
    flat = line("Beat", 210, 130, 20, 10)
    invalid = {"text": "Beat", "words": [{"text": "Beat", "rect": bad_rect}]}
    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: picture)
    monkeypatch.setattr(native, "_picture_records", lambda *args: [good, flat, invalid])
    monkeypatch.setattr(native, "_target_records", no_live_clip_captures)
    records = native._target_records(Window(), 1, (200, 100, 300, 150))
    assert len(records) == 2  # The same real word from both OCR scales.
    assert all(record["rect"] == good["rect"] for record in records)


def test_target_ocr_still_rejects_conflicting_nonflat_locations(monkeypatch, no_live_clip_captures):
    from PIL import Image, ImageGrab

    grid = native.Grid((0, 0, 1000, 500), 200, 100, 56, {1: (100, 150)}, 200)
    picture = Image.new("RGB", (267, 54), (51, 51, 51))
    picture.putpixel((15, 14), (52, 51, 51))
    picture.putpixel((15, 34), (51, 52, 51))
    first = line("Beat", 214, 105, 20, 10)
    second = line("Beat", 214, 125, 20, 10)
    readings = iter([[first], [second]])
    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: picture)
    monkeypatch.setattr(native, "_picture_records", lambda *args: next(readings))
    monkeypatch.setattr(native, "_target_records", no_live_clip_captures)
    with pytest.raises(RuntimeError, match="conflicting clip locations"):
        native._target_clip(Window(), 1, grid, [], "Beat", 1, 1)
