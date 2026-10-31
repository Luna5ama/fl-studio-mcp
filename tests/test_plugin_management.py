"""Host API, native file and lifecycle boundaries without a live FL session."""

import contextlib
import json
import struct
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from fl_studio_mcp.tools import plugin_management as tools
from fl_studio_mcp.utils import plugin_management_windows as native
from fl_studio_mcp.utils.runtime_operations import LoadOperations

ROOT = Path(__file__).resolve().parents[1]
TARGET = {"kind": "effect", "index": 2, "slot_index": 1}


@pytest.fixture(autouse=True)
def isolated_preset_operations(monkeypatch):
    operations = LoadOperations()
    monkeypatch.setattr(native, "preset_operations", operations)
    monkeypatch.setattr(tools, "preset_operations", operations)
    return operations


def fst_bytes(payload=b"complete native plugin state"):
    return (
        b"FLhd"
        + struct.pack("<I", 6)
        + b"\x30\x00\x02\x00\x30\x00FLdt"
        + struct.pack("<I", len(payload))
        + payload
    )


class Host:
    """An independently scheduled FL API boundary with actual folder traversal."""

    def __init__(self, database):
        self.database = database
        self.channels = ["OldSynth", "OtherSynth"]
        self.effects = [[None] * 10 for _ in range(4)]
        self.effects[2][0] = "NeighborFX"
        self.track = 0
        self.muted = set()
        self.playing = False
        self.recording = False
        self.mix = {}
        self.values = {}
        self.focused_channel = 0
        self.auto_hide = 1
        self.actions = []
        self.pending = []
        self.selected = "Current project"
        self.expanded = set()
        self.children = {"": ["Current project", "Plugin database", "Packs"]}
        self.types = {"Current project": -101, "Plugin database": -1000, "Packs": -100}
        for path in sorted(database.rglob("*")):
            key = "Plugin database/" + path.relative_to(database).as_posix()
            self.children.setdefault(key.rsplit("/", 1)[0], []).append(key)
            self.types[key] = -100 if path.is_dir() else 4

    def visible(self, parent=""):
        result = []
        for key in self.children.get(parent, []):
            result.append(key)
            if key in self.expanded:
                result.extend(self.visible(key))
        return result

    def navigate(self, direction, shift):
        assert shift == 0
        nodes = self.visible()
        index = nodes.index(self.selected) + (1 if direction == 41 else -1)
        self.selected = nodes[max(0, min(len(nodes) - 1, index))]

    def focus(self, window):
        self.actions.append(("focus", window))
        self.pending.append(lambda: setattr(self, "track", 0))

    def flush(self):
        pending, self.pending = self.pending, []
        for fn in pending:
            fn()

    def name(self, index, slot, user_name, global_index):
        assert global_index is True and user_name in (0, 1)
        return self.channels[index] if slot == -1 else self.effects[index][slot]

    def parameters(self, index, slot):
        return self.values.setdefault(self.name(index, slot, 0, True), [0.125, 0.75])


@pytest.fixture
def controller(tmp_path, monkeypatch):
    database = tmp_path / "Presets" / "Plugin database"
    for kind in ("Effects", "Generators"):
        path = database / "MCP Presets" / kind / "state.fst"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fst_bytes())
    host = Host(database)
    modules = {
        "channels": types.SimpleNamespace(
            channelCount=lambda global_index: len(host.channels),
            channelNumber=lambda global_index: host.focused_channel,
            getChannelIndex=lambda index: index,
            getChannelName=lambda index, global_index: host.channels[index],
            isChannelMuted=lambda index, global_index: index in host.muted,
            getTargetFxTrack=lambda index, global_index: 1,
            muteChannel=lambda index, muted, global_index: (
                host.muted.add(index) if muted else host.muted.discard(index)
            ),
            selectOneChannel=lambda index, global_index: setattr(host, "focused_channel", index),
            showEditor=lambda *args: host.actions.append(("editor", args)),
            focusEditor=lambda *args: host.actions.append(("focusEditor", args)),
        ),
        "midi": types.SimpleNamespace(
            widBrowser=4,
            widMixer=0,
            widChannelRack=1,
            widPluginGenerator=7,
            FPT_Up=40,
            FPT_Down=41,
            FPT_ItemMenu=91,
            PME_System=1,
            PME_FromMIDI=32,
        ),
        "mixer": types.SimpleNamespace(
            trackCount=lambda: 5,
            trackNumber=lambda: host.track,
            getTrackName=lambda index: "Master" if index == 0 else f"Insert {index}",
            setActiveTrack=lambda index: setattr(host, "track", index),
            getPluginMixLevel=lambda index, slot: host.mix.get((index, slot), 1.0),
            setPluginMixLevel=lambda index, slot, value: host.mix.__setitem__((index, slot), value),
            isTrackSlotsEnabled=lambda index: False,
            focusEditor=lambda index, slot: host.actions.append(("editor", index, slot)),
            getActiveEffectIndex=lambda: (host.track, 0),
        ),
        "plugins": types.SimpleNamespace(
            isValid=lambda index, slot, global_index: (
                0 <= index < len(host.channels)
                if slot == -1
                else host.effects[index][slot] is not None
            ),
            getPluginName=host.name,
            getParamName=lambda param, index, slot, global_index: (
                "Param " + str(param) if param < 2 else ""
            ),
            getParamCount=lambda index, slot, global_index: len(host.parameters(index, slot)),
            getParamValue=lambda param, index, slot, global_index: host.parameters(index, slot)[
                param
            ],
        ),
        "transport": types.SimpleNamespace(
            globalTransport=lambda *args: host.actions.append(args),
            isPlaying=lambda: host.playing,
            isRecording=lambda: host.recording,
        ),
        "ui": types.SimpleNamespace(
            getFocusedNodeCaption=lambda: host.selected.rsplit("/", 1)[-1],
            getFocusedNodeFileType=lambda: host.types[host.selected],
            toggleBrowserNode=lambda value: (
                host.expanded.add(host.selected) if value else host.expanded.discard(host.selected)
            ),
            navigateBrowser=host.navigate,
            navigateBrowserTabs=lambda value: None,
            showWindow=host.focus,
            setFocused=host.focus,
            getFocused=lambda window: True,
            getFocusedFormID=lambda: host.focused_channel,
            getFocusedPluginName=lambda: host.channels[host.focused_channel],
            isBrowserAutoHide=lambda: host.auto_hide,
            setBrowserAutoHide=lambda value: setattr(host, "auto_hide", value),
            isInPopupMenu=lambda: False,
        ),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    module = types.ModuleType("plugin_controller_under_test")
    source = ROOT / "fl_controller" / "mcp_plugin_management.py"
    exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), module.__dict__)
    module.configure(tmp_path / "Settings" / "Hardware" / "FLStudioMCP")
    return module, host


def dispatch(module, command, **params):
    return module.dispatch("pluginManagement." + command, params)


def begin(module, **overrides):
    return dispatch(
        module,
        "beginPreset",
        **{**TARGET, "relative_id": "MCP Presets/Effects/state.fst", **overrides},
    )


def ready(module, host):
    for _ in range(600):
        host.flush()
        module.on_idle()
        if (
            module._operation["status"] != "loading"
            or module._operation["stage"] == "awaiting_drag"
        ):
            return module._operation
    pytest.fail("Browser did not reach the explicit destination")


def test_inventory_distinguishes_unknown_individual_bypass_from_track_switch(controller):
    module, host = controller
    result = dispatch(module, "inventory")
    assert len(result["instruments"]) == 2
    slot = result["mixer_tracks"][0]["slots"][0]
    assert slot["plugin_name"] == "NeighborFX"
    assert slot["enabled"] is None
    assert slot["track_effects_enabled"] is False
    assert len(dispatch(module, "inventory", include_empty=True)["mixer_tracks"]) == 4
    assert host.actions == []


def test_api_mutations_read_back_actual_host_values(controller):
    module, host = controller
    target = dict(TARGET, slot_index=0, expected_plugin="NeighborFX")
    assert dispatch(module, "setMix", **target, value=0.4)["mix_level"] == 0.4
    assert host.mix[2, 0] == 0.4
    assert (
        dispatch(module, "setInstrumentEnabled", kind="generator", index=1, enabled=False)[
            "enabled"
        ]
        is False
    )
    assert host.muted == {1}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 1.1, True])
def test_invalid_wet_values_do_not_reach_host(controller, value):
    module, host = controller
    with pytest.raises(ValueError):
        dispatch(module, "setMix", **dict(TARGET, slot_index=0), value=value)
    assert host.mix == {}


@pytest.mark.parametrize(
    "relative",
    [
        "../state.fst",
        "MCP Presets/Effects/../state.fst",
        "MCP Presets/Generators/state.fst",
        "MCP Presets/Effects/no.fst",
    ],
)
def test_invalid_fst_identity_has_no_ui_side_effect(controller, relative):
    module, host = controller
    with pytest.raises(ValueError):
        begin(module, relative_id=relative)
    assert host.actions == [] and not module.is_busy()


def test_occupied_target_needs_explicit_replacement(controller):
    module, host = controller
    with pytest.raises(ValueError, match="replace_existing"):
        begin(module, slot_index=0)
    assert host.actions == []


def test_exact_empty_slot_and_saved_parameters_are_verified(controller):
    module, host = controller
    state = begin(module, expected_loaded_plugin="RestoredFX", expected_values=[0.125, 0.75])
    assert ready(module, host)["stage"] == "awaiting_drag"
    assert host.track == 2  # Must be selected after queued focus changes have settled.
    dispatch(module, "confirm", operation_id=state["operation_id"])
    host.effects[2][1] = "RestoredFX"
    dispatch(module, "acknowledgeDrop", operation_id=state["operation_id"])
    for _ in range(3):
        module.on_idle()
    result = dispatch(module, "status", operation_id=state["operation_id"])
    assert result["status"] == "loaded" and result["activation_issued"]
    assert result["verification"] == "published_parameters"
    assert host.effects[2][:2] == ["NeighborFX", "RestoredFX"]
    assert host.effects[0] == [None] * 10
    assert host.auto_hide == 1
    assert not any(key.startswith("_") for key in result)


def test_changed_target_is_rejected_before_native_drop(controller):
    module, host = controller
    state = begin(module)
    ready(module, host)
    host.effects[2][1] = "SomeoneElse"
    with pytest.raises(RuntimeError, match="destination changed"):
        dispatch(module, "confirm", operation_id=state["operation_id"])
    assert not module._operation.get("_issued")


def test_confirm_rejects_selected_track_drift_and_stale_operation_id(controller):
    module, host = controller
    state = begin(module)
    ready(module, host)
    host.track = 1
    with pytest.raises(RuntimeError, match="track changed"):
        dispatch(module, "confirm", operation_id=state["operation_id"])
    with pytest.raises(ValueError, match="Unknown"):
        dispatch(module, "confirm", operation_id="stale")
    assert not module._operation.get("_issued")


def test_loading_blocks_second_mutation(controller):
    module, _ = controller
    begin(module)
    with pytest.raises(RuntimeError, match="active"):
        dispatch(module, "setMix", **dict(TARGET, slot_index=0), value=0.1)


@pytest.mark.parametrize("issued", [False, True])
def test_timeout_distinguishes_unissued_failure_from_unknown_activation(controller, issued):
    module, host = controller
    state = begin(module)
    if issued:
        ready(module, host)
        dispatch(module, "confirm", operation_id=state["operation_id"])
    module._operation["_deadline"] = time.monotonic() - 1
    module.on_idle()
    assert module._operation["status"] == ("unknown" if issued else "failed")
    assert module._operation["activation_may_have_occurred"] is issued
    assert host.effects[2][1] is None and host.auto_hide == 1


@pytest.mark.parametrize("drop_before_cancel", [False, True])
def test_cancellation_after_activation_observes_late_completion(controller, drop_before_cancel):
    module, host = controller
    state = begin(module)
    ready(module, host)
    dispatch(module, "confirm", operation_id=state["operation_id"])
    if drop_before_cancel:
        dispatch(module, "acknowledgeDrop", operation_id=state["operation_id"])
    result = dispatch(module, "cancel", operation_id=state["operation_id"])
    assert result["status"] == "loading" and result["cancellation_requested"]
    assert not result.get("error") and result["cancellation_detail"]
    assert result["inspect_before_retrying"] is True
    assert module.is_busy()
    host.effects[2][1] = "LateFX"
    if not drop_before_cancel:
        dispatch(module, "acknowledgeDrop", operation_id=state["operation_id"])
    for _ in range(3):
        module.on_idle()
    terminal = dispatch(module, "status", operation_id=state["operation_id"])
    assert terminal["status"] == "loaded" and terminal["success"] is True
    assert terminal["cancellation_requested"] is True
    assert not terminal.get("error") and terminal["inspect_before_retrying"] is False


def test_neighbor_mutation_cannot_be_reported_as_success(controller):
    module, host = controller
    state = begin(module)
    ready(module, host)
    dispatch(module, "confirm", operation_id=state["operation_id"])
    host.effects[2][1] = "RestoredFX"
    host.effects[2][0] = "AccidentalFX"
    dispatch(module, "acknowledgeDrop", operation_id=state["operation_id"])
    for _ in range(3):
        module.on_idle()
    assert module._operation["status"] == "unknown"
    assert "neighboring" in module._operation["error"]


@pytest.mark.parametrize("damaged", [b"{}", b"FLhd", fst_bytes()[:-1], fst_bytes() + b"extra"])
def test_full_fst_validation_rejects_parameter_json_and_partial_chunks(tmp_path, damaged):
    path = tmp_path / "broken.fst"
    path.write_bytes(damaged)
    with pytest.raises(ValueError):
        native.validate_fst(path)


def test_full_fst_validation_preserves_private_payload(tmp_path):
    path = tmp_path / "state.fst"
    path.write_bytes(fst_bytes(b"private VST chunk\x00\xff"))
    result = native.validate_fst(path)
    assert result["size"] == path.stat().st_size and len(result["sha256"]) == 64


class Connection:
    def __init__(self, handler):
        self.handler = handler
        self.commands = []

    def send_command(self, command, params):
        self.commands.append((command, params))
        if command in ("pluginManagement.prepareBrowser", "pluginManagement.getBrowserState"):
            return {
                "success": True,
                "focused": True,
                "form_id": 4,
                "browser_form_id": 4,
                "popup_open": False,
                "node_caption": "Current project",
            }
        return self.handler(command, params)

    def transaction(self):
        return contextlib.nullcontext()


class QuietWindows:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def require_foreground(self):
        pass

    def press(self, keys):
        assert keys == "ctrl+r"

    def children(self, **kwargs):
        assert kwargs.get("class_name") == "TDataBrowserTree"
        return [7]

    def click_text(self, hwnd, text):
        assert hwnd == 7 and text == "Current project"


def snapshot(name="FX", fingerprint="stable", occupied=True):
    return {
        "success": True,
        "occupied": occupied,
        "plugin_name": name,
        "display_name": name,
        "parameter_count": 2,
        "parameter_values": [0.125, 0.75],
        "parameter_fingerprint": fingerprint,
        "parameters_complete": True,
        "mix_level": 1.0,
    }


@pytest.mark.parametrize(
    "status,issued,retained",
    [("loaded", True, False), ("failed", False, False), ("unknown", True, True)],
)
def test_staged_full_fst_is_retained_when_late_activation_is_uncertain(
    tmp_path, monkeypatch, status, issued, retained
):
    path = tmp_path / "source.fst"
    path.write_bytes(fst_bytes())
    conn = Connection(
        lambda command, params: (
            snapshot(occupied=False)
            if command.endswith("snapshot")
            else {"success": True, "operation_id": "preset-1", "status": "loading"}
        )
    )
    monkeypatch.setattr(native, "FLWindows", QuietWindows)
    monkeypatch.setattr(
        native,
        "_poll",
        lambda *args: {
            "success": status == "loaded",
            "status": status,
            "activation_issued": issued,
        },
    )
    result = native.load_preset(conn, TARGET, str(path), tmp_path)
    staged = list((tmp_path / "Presets").rglob("*.fst"))
    assert bool(staged) is retained
    assert path.read_bytes() == fst_bytes()
    if retained:
        assert result["staged_preset"] == str(staged[0])


def test_invalid_or_occupied_load_never_opens_ui(tmp_path, monkeypatch):
    path = tmp_path / "source.fst"
    path.write_bytes(fst_bytes())
    monkeypatch.setattr(native, "FLWindows", lambda: pytest.fail("No GUI action was authorized"))
    conn = Connection(lambda *args: snapshot())
    with pytest.raises(ValueError, match="replace_existing"):
        native.load_preset(conn, TARGET, str(path), tmp_path)
    assert not (tmp_path / "Presets").exists()


def test_host_failure_status_is_preserved_instead_of_raised():
    result = {
        "success": False,
        "status": "unknown",
        "activation_issued": True,
        "error": "late plugin load",
    }
    conn = Connection(lambda *args: result)
    assert native._poll(conn, "id", 1) is result


def test_save_uses_complete_native_fst_and_manifest_not_parameter_json(tmp_path, monkeypatch):
    destination = tmp_path / "saved.fst"
    conn = Connection(lambda *args: snapshot())
    monkeypatch.setattr(native, "FLWindows", QuietWindows)
    monkeypatch.setattr(native, "_slot_menu", lambda *args: None)
    monkeypatch.setattr(native, "_choose", lambda *args: None)
    monkeypatch.setattr(native, "_choose_save_preset", lambda *args: None)
    monkeypatch.setattr(native, "_save_dialog", lambda windows, path: path.write_bytes(fst_bytes()))
    monkeypatch.setattr(native, "wait_fst", lambda path, timeout: native.validate_fst(path))
    result = native.save_preset(conn, TARGET, str(destination))
    assert result["status"] == "saved"
    assert destination.read_bytes() == fst_bytes()
    manifest = json.loads(destination.with_suffix(".fst.mcp.json").read_text())
    assert manifest["sha256"] == native.validate_fst(destination)["sha256"]
    assert manifest["parameter_values"] == [0.125, 0.75]
    assert result["hidden_vst_state_independently_verified"] is False


def test_save_target_drift_preserves_existing_destination(tmp_path, monkeypatch):
    destination = tmp_path / "saved.fst"
    destination.write_bytes(b"preserve me")
    count = 0

    def respond(command, params):
        nonlocal count
        if command.endswith("snapshot"):
            count += 1
        return snapshot(fingerprint="stable" if count < 2 else "changed")

    monkeypatch.setattr(native, "FLWindows", QuietWindows)
    monkeypatch.setattr(native, "_slot_menu", lambda *args: None)
    monkeypatch.setattr(native, "_choose", lambda *args: None)
    monkeypatch.setattr(native, "_choose_save_preset", lambda *args: None)
    monkeypatch.setattr(native, "_save_dialog", lambda windows, path: path.write_bytes(fst_bytes()))
    monkeypatch.setattr(native, "wait_fst", lambda path, timeout: native.validate_fst(path))
    with pytest.raises(RuntimeError, match="Source plugin changed"):
        native.save_preset(Connection(respond), TARGET, str(destination), overwrite=True)
    assert destination.read_bytes() == b"preserve me"


@pytest.mark.parametrize("source_changed,load_success", [(False, False), (True, True)])
def test_copy_then_delete_keeps_source_until_full_destination_verification(
    tmp_path, monkeypatch, source_changed, load_success
):
    source = dict(TARGET, slot_index=0)
    count = 0

    def respond(command, params):
        nonlocal count
        if command == "pluginManagement.setMix":
            return {"success": True}
        assert command == "pluginManagement.snapshot"
        if params["slot_index"] == 1:
            return snapshot(occupied=False)
        count += 1
        return snapshot(fingerprint="changed" if source_changed and count > 1 else "stable")

    monkeypatch.setattr(tools, "get_user_data_dir", lambda: tmp_path)
    monkeypatch.setattr(tools, "read_effect_enabled", lambda *args: {"enabled": True})
    monkeypatch.setattr(tools, "set_effect_enabled", lambda *args: {"enabled": True})
    monkeypatch.setattr(tools, "save_preset", lambda *args, **kwargs: {"success": True})
    monkeypatch.setattr(
        tools,
        "load_preset",
        lambda *args, **kwargs: {
            "success": load_success,
            "status": "loaded" if load_success else "unknown",
        },
    )
    monkeypatch.setattr(
        tools, "delete_plugin", lambda *args: pytest.fail("Source must be retained")
    )
    result = tools._copy_effect(Connection(respond), source, TARGET, 60, delete_source=True)
    assert not result["success"]
    assert result["status"] == ("partial" if source_changed else "unknown")


def test_public_tool_validation_rejects_invalid_targets_before_connection(monkeypatch):
    registry = {}

    class MCP:
        def tool(self):
            return lambda fn: registry.setdefault(fn.__name__, fn)

    tools.register_plugin_management_tools(MCP())
    monkeypatch.setattr(
        tools, "get_connection", lambda: pytest.fail("Invalid input reached MIDI/UI")
    )
    assert registry["fl_delete_plugin"]("effect", 0, "FX", slot_index=10)["success"] is False
    assert registry["fl_copy_effect"](0, 0, 1, 0, "FX", timeout=-1)["success"] is False
    assert registry["fl_set_plugin_enabled"]("generator", 0, "yes")["success"] is False


def test_native_reorder_uses_adjacent_swaps_and_verifies_every_slot(monkeypatch):
    slots = [snapshot(name=f"FX {n}") for n in range(10)]
    before = [item["plugin_name"] for item in slots]
    active = 0

    def respond(command, params):
        nonlocal active
        if command.endswith("trackSlots"):
            return {"success": True, "slots": copy_slots()}
        if command.endswith("snapshot"):
            return dict(slots[params["slot_index"]], success=True)
        if command.endswith("prepareTarget"):
            active = params["slot_index"]
        return {"success": True}

    def copy_slots():
        return [dict(item) for item in slots]

    def choose(windows, labels):
        assert labels[0].casefold() == "move down"
        slots[active], slots[active + 1] = slots[active + 1], slots[active]

    monkeypatch.setattr(native, "FLWindows", QuietWindows)
    monkeypatch.setattr(native, "_slot_menu", lambda *args: None)
    monkeypatch.setattr(native, "_choose", choose)
    result = native.move_effect_within_track(Connection(respond), 2, 0, 2, "FX 0")
    assert [item["plugin_name"] for item in slots] == before[1:3] + before[:1] + before[3:]
    assert result["automation_links_preserved"] is True


@pytest.mark.parametrize("neighbor_changed", [False, True])
def test_native_delete_verifies_explicit_slot_and_neighbors(monkeypatch, neighbor_changed):
    slots = [snapshot(name=f"FX {n}") for n in range(10)]

    def respond(command, params):
        if command.endswith("snapshot"):
            return slots[params["slot_index"]]
        if command.endswith("trackSlots"):
            return {"success": True, "slots": [dict(item) for item in slots]}
        return {"success": True}

    def choose(windows, labels):
        if labels[0] == "None":
            slots[1] = snapshot(name=None, occupied=False)
            if neighbor_changed:
                slots[0] = snapshot(name="Accidental replacement")

    monkeypatch.setattr(native, "FLWindows", QuietWindows)
    monkeypatch.setattr(native, "_slot_menu", lambda *args: None)
    monkeypatch.setattr(native, "_choose", choose)
    monkeypatch.setattr(native, "_choose_delete", lambda windows: choose(windows, ("None",)))
    monkeypatch.setattr(native, "_confirm_plugin_delete", lambda windows, kind: False)
    if neighbor_changed:
        with pytest.raises(RuntimeError, match="neighboring effect"):
            native.delete_plugin(Connection(respond), TARGET)
    else:
        assert native.delete_plugin(Connection(respond), TARGET)["status"] == "deleted"
    assert slots[1]["occupied"] is False


@pytest.mark.parametrize("readable", [False, True])
def test_bypass_reads_host_led_and_fails_before_click_on_unreadable_theme(monkeypatch, readable):
    from PIL import Image, ImageGrab

    enabled = True
    clicks = []

    class Windows(QuietWindows):
        def click(self, point):
            nonlocal enabled
            clicks.append(point)
            enabled = not enabled

    def image(**kwargs):
        result = Image.new("RGB", (35, 250), (20, 20, 20))
        if readable:
            for slot in range(10):
                if slot != 1 or enabled:
                    for y in range(24 + 20 * slot, 27 + 20 * slot):
                        for x in range(19, 22):
                            result.putpixel((x, y), (30, 180, 30))
        return result

    monkeypatch.setattr(native, "FLWindows", Windows)
    monkeypatch.setattr(native, "_track", lambda *args: [])
    monkeypatch.setattr(native, "_assert_target", lambda *args: None)
    monkeypatch.setattr(
        native,
        "_slot_layout",
        lambda *args: {
            "bounds": (0, 0, 400, 250),
            "crop": (40, 0, 400, 250),
            "first": 25,
            "pitch": 20,
            "y": 45,
        },
    )
    monkeypatch.setattr(ImageGrab, "grab", image)
    conn = Connection(lambda *args: {"success": True})
    if readable:
        result = native.set_effect_enabled(conn, TARGET, False)
        assert result["enabled"] is False and result["enabled_source"] == "mixer_enable_led"
        assert clicks == [(385, 45)]
        assert native.read_effect_enabled(conn, TARGET)["enabled"] is False
        assert len(clicks) == 1  # Reading does not toggle a disabled effect.
    else:
        with pytest.raises(RuntimeError, match="independently read"):
            native.set_effect_enabled(conn, TARGET, False)
        assert clicks == []


def test_interrupted_issued_load_returns_observable_operation_and_retains_fst(
    tmp_path, monkeypatch
):
    path = tmp_path / "source.fst"
    path.write_bytes(fst_bytes())

    def respond(command, params):
        if command.endswith("snapshot"):
            return snapshot(occupied=False)
        if command.endswith("cancel"):
            return {
                "success": True,
                "status": "loading",
                "activation_issued": True,
                "cancellation_requested": True,
            }
        return {"success": True, "operation_id": "late-1", "status": "loading"}

    monkeypatch.setattr(native, "FLWindows", QuietWindows)
    monkeypatch.setattr(
        native, "_poll", lambda *args: {"status": "loading", "stage": "awaiting_drag"}
    )
    monkeypatch.setattr(
        native,
        "_drag_to_target",
        lambda *args: (_ for _ in ()).throw(RuntimeError("Foreground changed after issuing drop")),
    )
    result = native.load_preset(Connection(respond), TARGET, str(path), tmp_path)
    assert result["status"] == "unknown" and result["operation_id"] == "late-1"
    assert result["runner_active"] is False
    assert result["cancellation_requested"] and result["inspect_before_retrying"]
    assert Path(result["staged_preset"]).is_file()


def test_unrelated_manifest_cannot_claim_parameter_verification(tmp_path, monkeypatch):
    path = tmp_path / "source.fst"
    path.write_bytes(fst_bytes())
    path.with_suffix(".fst.mcp.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "sha256": "wrong hash",
                "plugin_name": "WrongPlugin",
                "parameter_values": [0.0],
                "parameters_complete": True,
            }
        )
    )
    captured = {}

    def respond(command, params):
        if command.endswith("snapshot"):
            return snapshot(occupied=False)
        captured.update(params)
        return {"success": True, "operation_id": "preset-1", "status": "loading"}

    monkeypatch.setattr(native, "FLWindows", QuietWindows)
    monkeypatch.setattr(
        native,
        "_poll",
        lambda *args: {"status": "failed", "success": False, "activation_issued": False},
    )
    native.load_preset(Connection(respond), TARGET, str(path), tmp_path)
    assert captured["expected_values"] is None
    assert captured["expected_loaded_plugin"] is None


def test_instrument_menu_requires_exact_focused_editor(controller, monkeypatch):
    module, host = controller
    monkeypatch.setattr(module.ui, "getFocusedFormID", lambda: -1)
    with pytest.raises(RuntimeError, match="editor is not focused"):
        dispatch(module, "assertEditor", kind="generator", index=0)
    assert host.actions == []


@pytest.mark.parametrize("issued", [False, True])
def test_controller_deinit_restores_browser_without_undoing_issued_load(controller, issued):
    module, host = controller
    state = begin(module)
    if issued:
        ready(module, host)
        dispatch(module, "confirm", operation_id=state["operation_id"])
    else:
        module.on_idle()
    assert host.auto_hide == 0
    staged = host.database / "MCP Presets" / "Effects" / "state.fst"
    module.on_deinit()
    assert host.auto_hide == 1
    assert module._steps is None and not module.is_busy()
    assert module._operation["status"] == ("unknown" if issued else "cancelled")
    assert module._operation["activation_may_have_occurred"] is issued
    assert staged.is_file() and host.effects[2][1] is None


def test_cleanup_does_not_override_browser_preference_after_operation_ended(controller):
    module, host = controller
    state = begin(module)
    module.on_idle()
    dispatch(module, "cancel", operation_id=state["operation_id"])
    assert host.auto_hide == 1
    host.auto_hide = 0  # The user changes the preference after completion.
    module.on_deinit()
    assert host.auto_hide == 0


def test_deinit_tolerates_host_ui_teardown_and_records_cleanup_error(controller, monkeypatch):
    module, host = controller
    begin(module)
    module.on_idle()

    def unavailable(value):
        raise RuntimeError("UI already closed")

    monkeypatch.setattr(module.ui, "setBrowserAutoHide", unavailable)
    module.on_deinit()
    assert module._steps is None and not module.is_busy()
    assert module._operation["cleanup_error"] == "UI already closed"


def management_registry():
    registry = {}

    class MCP:
        def tool(self):
            return lambda fn: registry.setdefault(fn.__name__, fn)

    tools.register_plugin_management_tools(MCP())
    return registry


def test_active_preset_cancel_and_status_bypass_held_midi_transaction(
    tmp_path, monkeypatch, isolated_preset_operations
):
    source = tmp_path / "source.fst"
    source.write_bytes(fst_bytes())
    registry = management_registry()
    observed = threading.Event()
    connection_lock = threading.RLock()
    state = {"status": "loading"}
    results = []
    runner_ident = []
    connection_calls = []

    class LockedConnection:
        def transaction(self):
            return connection_lock

        def send_command(self, command, params):
            assert threading.get_ident() == runner_ident[0], "Tool waited on the MIDI transaction"
            with connection_lock:
                if command.endswith(("prepareBrowser", "getBrowserState")):
                    return {
                        "success": True,
                        "focused": True,
                        "form_id": 4,
                        "browser_form_id": 4,
                        "popup_open": False,
                        "node_caption": "Current project",
                    }
                if command.endswith("snapshot"):
                    return snapshot(occupied=False)
                if command.endswith("beginPreset"):
                    return {
                        "success": True,
                        "operation_id": "concurrent-1",
                        "status": "loading",
                        "stage": "browser",
                        "activation_issued": False,
                    }
                if command.endswith("cancel"):
                    state["status"] = "cancelled"
                observed.set()
                return {
                    "success": state["status"] == "loading",
                    "operation_id": "concurrent-1",
                    "status": state["status"],
                    "stage": "browser",
                    "activation_issued": False,
                }

    conn = LockedConnection()

    def connection():
        connection_calls.append(threading.get_ident())
        return conn

    monkeypatch.setattr(tools, "get_connection", connection)
    monkeypatch.setattr(tools, "get_user_data_dir", lambda: tmp_path)
    monkeypatch.setattr(native, "FLWindows", QuietWindows)
    monkeypatch.setattr(
        native, "_drag_to_target", lambda *args: pytest.fail("Cancelled load activated")
    )

    def run():
        runner_ident.append(threading.get_ident())
        results.append(registry["fl_load_plugin_preset"](str(source), "effect", 2, slot_index=1))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert observed.wait(2)
        assert not connection_lock.acquire(blocking=False)
        status = registry["fl_get_plugin_preset_status"]("concurrent-1")
        assert status["status"] == "loading" and status["runner_active"]
        history = registry["fl_list_plugin_preset_operations"]()
        assert history["operations"][0]["operation_id"] == "concurrent-1"
        request = registry["fl_cancel_plugin_preset_load"]("concurrent-1")
        assert request["status"] == "cancellation_requested"
        assert isolated_preset_operations.cancelled("concurrent-1")
        assert len(connection_calls) == 1  # Only the runner acquired the connection.
    finally:
        isolated_preset_operations.cancel("concurrent-1")
        thread.join(2)
    assert not thread.is_alive()
    assert results[0]["status"] == "cancelled" and not results[0]["runner_active"]
    final = isolated_preset_operations.get("concurrent-1")
    assert final["status"] == "cancelled" and not final["runner_active"]
    assert not list((tmp_path / "Presets").rglob("*.fst"))


def test_cancellation_request_ack_does_not_end_preset_observation(isolated_preset_operations):
    operations = isolated_preset_operations
    operations.begin("issued-1", runner_active=True, activation_issued=True)
    operations.cancel("issued-1")
    replies = iter(
        [
            {"success": True, "status": "cancellation_requested", "activation_issued": True},
            {"success": True, "status": "loaded", "activation_issued": True},
        ]
    )
    conn = Connection(lambda *args: next(replies))
    result = native._poll(conn, "issued-1", 1)
    assert result["status"] == "loaded"
    assert [command for command, params in conn.commands] == [
        "pluginManagement.cancel",
        "pluginManagement.status",
    ]


def test_preset_cancel_is_rechecked_after_slow_drag_target_resolution(
    tmp_path, monkeypatch, isolated_preset_operations
):
    from PIL import Image, ImageGrab

    operations = isolated_preset_operations
    operations.begin("before-confirm", runner_active=True)
    calls = []
    bounds = (0, 0, 100, 100)

    class Windows:
        def children(self, **kwargs):
            return [1]

        def rect(self, hwnd):
            return bounds

        def require_foreground(self):
            pass

        def _check_point(self, hwnd, point):
            pass

    async def rows(image):
        return [{"text": "mcp_test", "box": (10, 10, 40, 20)}]

    def layout(*args):
        operations.cancel("before-confirm")
        return {
            "window": 2,
            "bounds": bounds,
            "crop": bounds,
            "row": {"box": (10, 40, 40, 50)},
            "y": 45,
        }

    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: Image.new("RGB", (100, 100)))
    monkeypatch.setattr(native, "_ocr_lines", rows)
    monkeypatch.setattr(native, "_track", lambda *args: [])
    monkeypatch.setattr(native, "_slot_layout", layout)
    conn = Connection(lambda *args: calls.append(args) or {"success": True})
    with pytest.raises(RuntimeError, match="Cancelled before preset activation"):
        native._drag_to_target(Windows(), conn, TARGET, "mcp_test", "before-confirm")
    assert calls == []  # No confirm or actual mouse press.


def test_runner_exception_leaves_unknown_cache_with_runner_inactive(
    tmp_path, monkeypatch, isolated_preset_operations
):
    path = tmp_path / "source.fst"
    path.write_bytes(fst_bytes())

    def respond(command, params):
        if command.endswith("snapshot"):
            return snapshot(occupied=False)
        if command.endswith("beginPreset"):
            return {"success": True, "operation_id": "exception-1", "status": "loading"}
        raise RuntimeError("MIDI disconnected")

    monkeypatch.setattr(native, "FLWindows", QuietWindows)
    result = native.load_preset(Connection(respond), TARGET, str(path), tmp_path)
    assert result["status"] == "unknown" and result["runner_active"] is False
    cached = isolated_preset_operations.get("exception-1")
    assert cached["status"] == "unknown" and cached["runner_active"] is False
    assert Path(cached["staged_preset"]).is_file()


def test_inactive_preset_runner_can_observe_late_controller_completion(
    monkeypatch, isolated_preset_operations
):
    operations = isolated_preset_operations
    operations.begin("late-completion", runner_active=False)
    operations.update({"operation_id": "late-completion", "status": "unknown", "success": False})
    conn = Connection(
        lambda *args: {
            "success": True,
            "operation_id": "late-completion",
            "status": "loaded",
            "activation_issued": True,
        }
    )
    monkeypatch.setattr(tools, "get_connection", lambda: conn)
    result = management_registry()["fl_get_plugin_preset_status"]("late-completion")
    assert result["status"] == "loaded" and len(conn.commands) == 1
    assert operations.get("late-completion")["status"] == "loaded"


def test_preset_runner_finally_cleans_cache_after_base_exception(
    tmp_path, monkeypatch, isolated_preset_operations
):
    path = tmp_path / "source.fst"
    path.write_bytes(fst_bytes())

    def respond(command, params):
        if command.endswith("snapshot"):
            return snapshot(occupied=False)
        if command.endswith("beginPreset"):
            return {"success": True, "operation_id": "interrupt-1", "status": "loading"}
        raise KeyboardInterrupt()

    monkeypatch.setattr(native, "FLWindows", QuietWindows)
    with pytest.raises(KeyboardInterrupt):
        native.load_preset(Connection(respond), TARGET, str(path), tmp_path)
    cached = isolated_preset_operations.get("interrupt-1")
    assert cached["status"] == "unknown" and not cached["runner_active"]
    assert Path(cached["staged_preset"]).is_file()


def test_terminal_cached_preset_status_needs_no_midi(monkeypatch, isolated_preset_operations):
    operations = isolated_preset_operations
    operations.begin("done-1", runner_active=False)
    operations.update({"operation_id": "done-1", "status": "loaded", "success": True})
    monkeypatch.setattr(tools, "get_connection", lambda: pytest.fail("Terminal cache used MIDI"))
    registry = management_registry()
    assert registry["fl_get_plugin_preset_status"]("done-1")["status"] == "loaded"
    cancelled = registry["fl_cancel_plugin_preset_load"]("done-1")
    assert cancelled["status"] == "loaded" and not cancelled["cancellation_requested"]


def test_status_for_different_preset_operation_is_never_accepted(isolated_preset_operations):
    isolated_preset_operations.begin("ours", runner_active=True)
    conn = Connection(lambda *args: {"success": True, "operation_id": "other", "status": "loaded"})
    with pytest.raises(RuntimeError, match="different operation"):
        native._poll(conn, "ours", 1)
    assert isolated_preset_operations.get("ours")["status"] == "loading"


def test_confirm_cannot_verify_old_target_before_mouse_release_ack(controller):
    module, host = controller
    host.effects[2][1] = "ExistingFX"
    state = begin(
        module,
        replace_existing=True,
        expected_loaded_plugin="ExistingFX",
        expected_values=[0.125, 0.75],
    )
    ready(module, host)
    dispatch(module, "confirm", operation_id=state["operation_id"])
    for _ in range(6):
        module.on_idle()
    assert module._operation["status"] == "loading"
    assert module._operation["stage"] == "awaiting_drop"
    assert module._operation["drop_acknowledged"] is False
    # Even a new host-visible instance cannot be accepted before native input finishes.
    host.effects[2][1] = "DifferentFX"
    for _ in range(4):
        module.on_idle()
    assert module._operation["status"] == "loading"


@pytest.mark.parametrize("expected_values", [None, [0.125, 0.75]])
def test_same_plugin_unchanged_after_drop_is_unknown_even_if_manifest_matches(
    controller, expected_values
):
    module, host = controller
    host.effects[2][1] = "ExistingFX"
    state = begin(module, replace_existing=True, expected_values=expected_values)
    ready(module, host)
    dispatch(module, "confirm", operation_id=state["operation_id"])
    dispatch(module, "acknowledgeDrop", operation_id=state["operation_id"])
    for _ in range(5):
        module.on_idle()
    assert module._operation["status"] == "loading"
    module._operation["_deadline"] = time.monotonic() - 1
    module.on_idle()
    result = dispatch(module, "status", operation_id=state["operation_id"])
    assert result["status"] == "unknown" and result["success"] is False
    assert result["verification"] == "no_observable_application_witness"
    assert result["observable_no_op"] is True
    assert result["hidden_state_independently_verified"] is False
    assert result["drop_acknowledged"] is True
    assert (host.database / "MCP Presets/Effects/state.fst").is_file()


def test_manifest_load_requires_change_from_before_and_then_expected_values(controller):
    module, host = controller
    host.effects[2][1] = "ExistingFX"
    state = begin(
        module,
        replace_existing=True,
        expected_loaded_plugin="ExistingFX",
        expected_values=[0.5, 0.75],
    )
    ready(module, host)
    dispatch(module, "confirm", operation_id=state["operation_id"])
    dispatch(module, "acknowledgeDrop", operation_id=state["operation_id"])
    host.values["ExistingFX"][0] = 0.3  # Changed, but not the saved preset yet.
    for _ in range(4):
        module.on_idle()
    assert module._operation["status"] == "loading"
    host.values["ExistingFX"][0] = 0.5
    for _ in range(3):
        module.on_idle()
    result = dispatch(module, "status", operation_id=state["operation_id"])
    assert result["status"] == "loaded"
    assert result["application_witness"] == "published_parameters_changed"
    assert result["verification"] == "published_parameters"
    assert result["hidden_state_independently_verified"] is False


def test_different_plugin_identity_after_ack_is_an_application_witness(controller):
    module, host = controller
    host.effects[2][1] = "ExistingFX"
    state = begin(module, replace_existing=True, expected_loaded_plugin="DifferentFX")
    ready(module, host)
    dispatch(module, "confirm", operation_id=state["operation_id"])
    host.effects[2][1] = "DifferentFX"
    dispatch(module, "acknowledgeDrop", operation_id=state["operation_id"])
    for _ in range(4):
        module.on_idle()
    assert module._operation["status"] == "loaded"
    assert module._operation["application_witness"] == "plugin_identity_changed"


def test_drop_ack_requires_this_operation_confirm_first(controller):
    module, host = controller
    state = begin(module)
    ready(module, host)
    with pytest.raises(RuntimeError, match="not confirmed"):
        dispatch(module, "acknowledgeDrop", operation_id=state["operation_id"])
    with pytest.raises(ValueError, match="Unknown"):
        dispatch(module, "acknowledgeDrop", operation_id="stale")
    assert module._operation["drop_acknowledged"] is False


def test_native_drop_ack_is_sent_only_after_successful_mouse_release(monkeypatch):
    from PIL import Image, ImageGrab

    events = []
    bounds = (0, 0, 100, 100)

    class Mouse:
        position = None

        def press(self, button):
            events.append("press")

        def release(self, button):
            events.append("release")

    class Windows:
        mouse = Mouse()

        def children(self, **kwargs):
            return [1]

        def rect(self, hwnd):
            return bounds

        def require_foreground(self):
            pass

        def _check_point(self, hwnd, point):
            pass

    async def rows(image):
        return [{"text": "mcp_test", "box": (10, 10, 40, 20)}]

    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: Image.new("RGB", (100, 100)))
    monkeypatch.setattr(native, "_ocr_lines", rows)
    monkeypatch.setattr(native, "_track", lambda *args: [])
    monkeypatch.setattr(
        native,
        "_slot_layout",
        lambda *args: {
            "window": 2,
            "bounds": bounds,
            "crop": bounds,
            "row": {"box": (10, 40, 40, 50)},
            "y": 45,
        },
    )
    monkeypatch.setattr(native.time, "sleep", lambda seconds: None)
    conn = Connection(lambda command, params: events.append(command) or {"success": True})
    native._drag_to_target(Windows(), conn, TARGET, "mcp_test", "successful-drop")
    assert events == [
        "pluginManagement.confirm",
        "press",
        "release",
        "pluginManagement.acknowledgeDrop",
    ]


@pytest.mark.parametrize("mismatched,extra_edit", [(False, False), (True, False), (False, True)])
def test_localized_fst_save_dialog_uses_observed_control_ids_and_exact_readback(
    tmp_path, mismatched, extra_edit
):
    ids = {10: 1001, 11: 1001, 20: 1, 21: 2, 30: 1001}
    clicks = []
    values = {}

    def get_id(hwnd):
        return ids[hwnd]

    class Windows:
        user = types.SimpleNamespace(GetDlgCtrlID=get_id)

        def wait_window(self, **kwargs):
            assert kwargs["class_name"] == "#32770"
            return 1

        def children(self, parent, class_name):
            assert parent == 1
            if class_name == "Edit":
                return [10, 11] if extra_edit else [10]
            if class_name == "Button":
                return [20, 21]
            return [30]  # Toolbar1001 must never be mistaken for filename Edit1001.

        def set_text(self, hwnd, text):
            values[hwnd] = text

        def text(self, hwnd):
            if hwnd in (20, 21):
                return "保存" if hwnd == 20 else "取消"
            return values[hwnd] + ("wrong" if mismatched else "")

        def rect(self, hwnd):
            assert hwnd == 20
            return (0, 0, 10, 10)

        def click(self, point):
            clicks.append(point)

    if mismatched or extra_edit:
        with pytest.raises(RuntimeError, match="readback|unique"):
            native._save_dialog(Windows(), tmp_path / "完整预设.fst")
        assert clicks == []
    else:
        native._save_dialog(Windows(), tmp_path / "完整预设.fst")
        assert clicks == [(5, 5)]


def test_controller_browser_preparation_reports_actual_focus_and_refuses_popup(
    controller, monkeypatch
):
    module, host = controller
    focus = {"ready": False, "form": 2}
    monkeypatch.setattr(module.ui, "getFocused", lambda window: focus["ready"])
    monkeypatch.setattr(module.ui, "getFocusedFormID", lambda: focus["form"])
    state = dispatch(module, "prepareBrowser")
    assert state["focused"] is False and state["form_id"] == 2
    assert host.actions == [("focus", 4), ("focus", 4)]
    focus.update(ready=True, form=4)
    assert dispatch(module, "getBrowserState")["focused"] is True
    assert dispatch(module, "getBrowserState")["browser_form_id"] == 4
    host.actions.clear()
    monkeypatch.setattr(module.ui, "isInPopupMenu", lambda: True)
    with pytest.raises(RuntimeError, match="open FL menu"):
        dispatch(module, "prepareBrowser")
    assert host.actions == []


@pytest.mark.parametrize(
    "case", ["playlist", "popup", "changed_before_shortcut", "foreground_changed"]
)
def test_browser_refresh_never_sends_contextual_shortcut_without_confirmed_focus(monkeypatch, case):
    ready_state = {
        "success": True,
        "focused": True,
        "form_id": 4,
        "browser_form_id": 4,
        "popup_open": False,
        "node_caption": "Current project",
    }
    wrong_state = dict(ready_state, focused=False, form_id=2)
    if case == "playlist":
        states = [wrong_state]
    elif case == "popup":
        states = [dict(ready_state, popup_open=True)]
    elif case == "changed_before_shortcut":
        states = [ready_state, wrong_state]
    else:
        states = [ready_state]
    clock = [0.0]
    keys = []

    class BrowserConnection:
        def send_command(self, command, params):
            clock[0] += 0.01
            if command.endswith("prepareBrowser"):
                return ready_state
            assert command.endswith("getBrowserState")
            return states.pop(0) if len(states) > 1 else states[0]

    class Windows:
        def require_foreground(self):
            if case == "foreground_changed":
                raise RuntimeError("Foreground changed")

        def press(self, shortcut):
            keys.append(shortcut)

    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(native.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + 0.01))
    with pytest.raises(RuntimeError, match="focus|Foreground"):
        native._refresh_browser(Windows(), BrowserConnection(), timeout=0.05)
    assert keys == []


def test_browser_refresh_waits_for_focus_then_rechecks_before_ctrl_r(monkeypatch):
    ready_state = {
        "success": True,
        "focused": True,
        "form_id": 4,
        "browser_form_id": 4,
        "popup_open": False,
        "node_caption": "Current project",
    }
    states = iter([dict(ready_state, focused=False, form_id=2)] + [ready_state] * 8)
    events = []

    class BrowserConnection:
        def send_command(self, command, params):
            events.append(command)
            if command.endswith("prepareBrowser"):
                return dict(ready_state, focused=False)
            return next(states)

    class Windows(QuietWindows):
        def require_foreground(self):
            events.append("foreground_confirmed")

        def press(self, shortcut):
            events.append(shortcut)

    monkeypatch.setattr(native.time, "sleep", lambda seconds: None)
    native._refresh_browser(Windows(), BrowserConnection())
    assert events[:6] == [
        "pluginManagement.prepareBrowser",
        "pluginManagement.getBrowserState",
        "pluginManagement.getBrowserState",
        "foreground_confirmed",
        "pluginManagement.getBrowserState",
        "ctrl+r",
    ]
    assert events[-2:] == ["pluginManagement.getBrowserState"] * 2


def test_preset_begins_only_after_confirmed_browser_refresh(tmp_path, monkeypatch):
    source = tmp_path / "source.fst"
    source.write_bytes(fst_bytes())
    events = []

    class Windows(QuietWindows):
        def press(self, shortcut):
            events.append(shortcut)

    def respond(command, params):
        if command.endswith("snapshot"):
            return snapshot(occupied=False)
        events.append(command)
        return {"success": True, "operation_id": "refreshed", "status": "loading"}

    monkeypatch.setattr(native, "FLWindows", Windows)
    monkeypatch.setattr(
        native,
        "_poll",
        lambda *args: {"success": False, "status": "failed", "activation_issued": False},
    )
    native.load_preset(Connection(respond), TARGET, str(source), tmp_path)
    assert events == ["ctrl+r", "pluginManagement.beginPreset"]


@pytest.mark.parametrize(
    "kind,covered", [("effect", False), ("generator", False), ("generator", True)]
)
def test_real_controller_dispatch_allows_production_drag_readback_while_busy(
    controller, monkeypatch, kind, covered
):
    from PIL import Image, ImageGrab

    module, host = controller
    target = TARGET if kind == "effect" else {"kind": "generator", "index": 0, "slot_index": -1}
    loaded_name = "LoadedFX" if kind == "effect" else "LoadedSynth"
    state = dispatch(
        module,
        "beginPreset",
        **dict(
            target,
            relative_id="MCP Presets/"
            + ("Effects" if kind == "effect" else "Generators")
            + "/state.fst",
            replace_existing=kind == "generator",
            expected_loaded_plugin=loaded_name,
            expected_values=[0.5, 0.75],
        ),
    )
    assert ready(module, host)["stage"] == "awaiting_drag"
    commands = []
    events = []
    target_reads = []
    bounds = {1: (0, 0, 100, 100), 2: (110, 0, 210, 100)}

    class ControllerConnection:
        def send_command(self, command, params):
            commands.append(command)
            try:
                result = module.dispatch(command, params)
                return {"success": True, **result}
            except Exception as exc:
                return {"success": False, "error": str(exc)}

    class Mouse:
        position = None

        def press(self, button):
            events.append("press")

        def release(self, button):
            events.append("release")
            if kind == "effect":
                host.effects[target["index"]][target["slot_index"]] = loaded_name
            else:
                host.channels[target["index"]] = loaded_name
            host.values[loaded_name] = [0.5, 0.75]
            # An actual OnIdle between release and ack must still not complete.
            module.on_idle()
            assert module._operation["status"] == "loading"

    class Windows:
        mouse = Mouse()

        def children(self, class_name, parent=None):
            if class_name == "TNewCaption":
                assert parent == 2 and covered
                return [3]
            return [1] if class_name == "TDataBrowserTree" else [2]

        def text(self, hwnd):
            assert hwnd == 3
            return "Channel rack"

        def click_text(self, hwnd, text):
            assert hwnd == 3 and text == "Channel rack"
            events.append("rack_forward")

        def rect(self, hwnd):
            return bounds[hwnd]

        def find_text(self, hwnd, text):
            assert kind == "generator" and hwnd == 2 and text == "OldSynth"
            target_reads.append(text)
            if covered and len(target_reads) == 1:
                raise RuntimeError("The exact channel is covered by its editor")
            assert not covered or events == ["rack_forward"]
            return (135, 45)

        def require_foreground(self):
            pass

        def _check_point(self, hwnd, point):
            box = bounds[hwnd]
            assert box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]

    async def rows(image):
        return [{"text": "state", "box": (10, 10, 40, 20)}]

    def layout(windows, slots, slot):
        assert len(slots) == 10 and slot == target["slot_index"]
        assert slots[0]["plugin_name"] == "NeighborFX"
        return {
            "window": 2,
            "bounds": bounds[2],
            "crop": bounds[2],
            "row": {"box": (10, 40, 40, 50)},
            "y": 45,
        }

    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: Image.new("RGB", (100, 100)))
    monkeypatch.setattr(native, "_ocr_lines", rows)
    monkeypatch.setattr(native, "_slot_layout", layout)
    monkeypatch.setattr(native.time, "sleep", lambda seconds: None)
    conn = ControllerConnection()
    assert native._assert_target(conn, target)["occupied"] is (kind == "generator")
    with pytest.raises(RuntimeError, match="active"):
        dispatch(module, "setMix", **dict(TARGET, slot_index=0), value=0.2)
    native._drag_to_target(Windows(), conn, target, "state", state["operation_id"])
    for _ in range(4):
        module.on_idle()
    result = dispatch(module, "status", operation_id=state["operation_id"])
    assert result["status"] == "loaded" and result["drop_acknowledged"]
    assert events == (["rack_forward"] if covered else []) + ["press", "release"]
    if kind == "generator":
        assert target_reads == ["OldSynth"] * (2 if covered else 1)
    assert "pluginManagement.assertTarget" in commands
    assert (
        "pluginManagement.trackSlots" if kind == "effect" else "pluginManagement.snapshot"
    ) in commands
    assert commands[-1] == "pluginManagement.acknowledgeDrop"


@pytest.mark.parametrize("transport_flag", ["playing", "recording"])
def test_preset_begin_rejects_active_transport_before_any_ui_action(controller, transport_flag):
    module, host = controller
    setattr(host, transport_flag, True)
    with pytest.raises(RuntimeError, match="Stop playback and recording"):
        begin(module)
    assert host.actions == [] and not module.is_busy()


@pytest.mark.parametrize("transport_flag", ["playing", "recording"])
def test_transport_started_after_drop_cannot_supply_parameter_application_witness(
    controller, transport_flag
):
    module, host = controller
    host.effects[2][1] = "ExistingFX"
    state = begin(module, replace_existing=True)
    ready(module, host)
    dispatch(module, "confirm", operation_id=state["operation_id"])
    dispatch(module, "acknowledgeDrop", operation_id=state["operation_id"])
    setattr(host, transport_flag, True)
    host.values["ExistingFX"][0] = 0.9  # Automation must not prove this FST applied.
    module.on_idle()
    assert module._operation["status"] == "unknown"
    assert module._operation["success"] is False
    assert "Playback or recording started" in module._operation["error"]


def test_transport_is_rechecked_immediately_before_confirm(controller):
    module, host = controller
    state = begin(module)
    ready(module, host)
    host.playing = True
    with pytest.raises(RuntimeError, match="Stop playback and recording"):
        dispatch(module, "confirm", operation_id=state["operation_id"])
    assert module._operation["activation_issued"] is False


def test_full_state_snapshot_includes_all_4240_host_vst_parameters(controller):
    module, host = controller
    host.values["NeighborFX"] = [0.0] * 4240
    host.values["NeighborFX"][-1] = 0.625
    host.track = 2
    result = dispatch(module, "assertTarget", kind="effect", index=2, slot_index=0)
    assert result["parameters_complete"] is True
    assert len(result["parameter_values"]) == 4240 and result["parameter_values"][-1] == 0.625
    assert native.FULL_STATE_PARAMETER_LIMIT == 16384


@pytest.mark.parametrize("row,accepted", [("Delete.„", True), ("Delete file", False)])
def test_real_fl26_delete_menu_punctuation_never_matches_delete_file(monkeypatch, row, accepted):
    from PIL import Image, ImageGrab

    clicks = []
    clock = [0.0]

    class Windows:
        def require_foreground(self):
            pass

        def windows(self, **kwargs):
            return [1]

        def rect(self, hwnd):
            return (0, 0, 200, 100)

        def _check_point(self, hwnd, point):
            assert hwnd == 1

        def click(self, point):
            clicks.append(point)

    async def rows(image):
        clock[0] += 1
        return [
            {"text": label, "box": (10, n * 20, 80, n * 20 + 10)}
            for n, label in enumerate(("Replace", "Browse presets", row))
        ]

    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: Image.new("RGB", (200, 100)))
    monkeypatch.setattr(native, "_ocr_lines", rows)
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(native.time, "sleep", lambda seconds: None)
    if accepted:
        native._choose_delete(Windows())
        assert clicks == [(45, 45)]
    else:
        with pytest.raises(RuntimeError, match="Cannot read plugin Delete"):
            native._choose_delete(Windows())
        assert clicks == []


@pytest.mark.parametrize(
    "kind,prompt,accepted",
    [
        ("effect", "The selected plugin is about to be deleted.", True),
        ("generator", "The selected channel is about to be deleted.", True),
        ("effect", "The selected channel is about to be deleted.", False),
        ("generator", "The selected plugin is about to be deleted.", False),
        ("effect", "Save changes to project?", False),
        ("generator", "Save changes to project?", False),
    ],
)
def test_plugin_delete_confirmation_requires_its_exact_prompt_before_clicking_0k(
    monkeypatch, kind, prompt, accepted
):
    from PIL import Image, ImageGrab

    clicks = []

    class Windows(QuietWindows):
        def windows(self, **kwargs):
            return [1]

        def rect(self, hwnd):
            return (100, 100, 400, 300)

        def _check_point(self, hwnd, point):
            assert hwnd == 1

        def click(self, point):
            clicks.append(point)

    async def rows(image):
        return [
            {"text": prompt, "box": (10, 20, 290, 40)},
            {"text": "0k", "box": (200, 170, 220, 190)},
            {"text": "Cancel", "box": (240, 170, 280, 190)},
        ]

    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: Image.new("RGB", (300, 200)))
    monkeypatch.setattr(native, "_ocr_lines", rows)
    if accepted:
        assert native._confirm_plugin_delete(Windows(), kind) is True
        assert clicks == [(310, 280)]
    else:
        with pytest.raises(RuntimeError, match="Unexpected confirmation"):
            native._confirm_plugin_delete(Windows(), kind)
        assert clicks == []


@pytest.mark.parametrize("target_index,accepted", [(6, True), (5, False)])
def test_generator_focus_maps_filtered_rack_form_id_to_exact_global_channel(
    controller, monkeypatch, target_index, accepted
):
    module, host = controller
    host.channels = ["Sampler", "Sampler2", "Sampler3", "Audio", "Audio2", "Speldosa", "Speldosa"]
    host.focused_channel = target_index
    monkeypatch.setattr(module.channels, "channelCount", lambda use_global: 7 if use_global else 4)
    monkeypatch.setattr(module.channels, "getChannelIndex", lambda local: [0, 1, 2, 6][local])
    monkeypatch.setattr(module.ui, "getFocusedFormID", lambda: 3)
    monkeypatch.setattr(module.ui, "getFocusedPluginName", lambda: "Speldosa")
    if accepted:
        result = dispatch(
            module, "assertEditor", kind="generator", index=6, expected_plugin="Speldosa"
        )
        assert result["status"] == "editor_verified"
        assert dispatch(module, "editorState")["mapped_global_index"] == 6
    else:
        with pytest.raises(RuntimeError, match="editor is not focused"):
            dispatch(module, "assertEditor", kind="generator", index=5, expected_plugin="Speldosa")
        assert host.actions == []


def test_native_wrapper_caption_uses_exact_channel_and_observed_first_button():
    class Windows:
        def children(self, class_name, parent=None):
            return [2] if parent == 1 else [1, 3]

        def windows(self, class_name):
            return []

        def text(self, hwnd):
            return {
                1: "Speldosa\ue5a0 (Insert 6)\ue5ff",
                2: "Speldosa\ue5a0 (Insert 6)\ue5ff",
                3: "Speldosa #2\ue5a0 (Insert 6)\ue5ff",
            }[hwnd]

        def rect(self, hwnd):
            return (523, 278, 1413, 569) if hwnd == 1 else (525, 280, 1411, 303)

    assert native._wrapper_menu_button(Windows(), "Speldosa") == (
        1,
        2,
        (523, 278, 1413, 569),
        (525, 280, 1411, 303),
        (535, 292),
    )


def test_native_wrapper_menu_rejects_ambiguous_same_named_instruments():
    class Windows:
        def children(self, class_name, parent=None):
            return [1, 2]

        def windows(self, class_name):
            return []

        def text(self, hwnd):
            return "Speldosa (Insert 6)"

    with pytest.raises(RuntimeError, match="exactly one wrapper"):
        native._wrapper_menu_button(Windows(), "Speldosa")


def test_native_wrapper_menu_uses_real_controller_identity_guard_not_generic_transport(controller):
    module, host = controller
    target = {"kind": "generator", "index": 0, "slot_index": -1}
    before = dispatch(module, "snapshot", **target, parameter_limit=16384, include_names=True)
    commands = []
    clicks = []

    class ControllerConnection:
        def send_command(self, command, params):
            commands.append(command)
            try:
                return {"success": True, **module.dispatch(command, params)}
            except Exception as exc:
                return {"success": False, "error": str(exc)}

    class Windows:
        def children(self, class_name, parent=None):
            return [2] if parent == 1 else [1]

        def windows(self, class_name):
            return []

        def text(self, hwnd):
            return "OldSynth (Insert 1)"

        def rect(self, hwnd):
            return (0, 0, 800, 400) if hwnd == 1 else (2, 2, 798, 25)

        def _check_point(self, hwnd, point):
            assert hwnd == 2 and point == (12, 14)

        def click(self, point):
            assert commands[-1] == "pluginManagement.assertEditor"
            clicks.append(point)

        def wait_window(self, **kwargs):
            assert kwargs["class_name"] == "TQuickPopupMenuWindow"
            return 3

    native._editor_menu(Windows(), ControllerConnection(), target, before)
    assert clicks == [(12, 14)]
    assert "pluginManagement.openMenu" not in commands
    assert not any(action and action[0] == 91 for action in host.actions)


def test_browser_refresh_initializes_null_selection_only_through_verified_visible_root():
    events = []
    selected = [""]

    class BrowserConnection:
        def send_command(self, command, params):
            events.append(command)
            return {
                "success": True,
                "focused": True,
                "form_id": 4,
                "browser_form_id": 4,
                "popup_open": False,
                "node_caption": selected[0],
                "node_type": -1,
            }

    class Windows:
        def require_foreground(self):
            pass

        def press(self, shortcut):
            assert shortcut == "ctrl+r"
            events.append(shortcut)

        def children(self, **kwargs):
            assert kwargs["class_name"] == "TDataBrowserTree"
            return [7]

        def click_text(self, hwnd, text):
            assert hwnd == 7 and text == "Current project"
            events.append("visible root selected")
            selected[0] = "Current project"

    native._refresh_browser(Windows(), BrowserConnection())
    assert events.index("ctrl+r") < events.index("visible root selected")
    assert events[-1] == "pluginManagement.getBrowserState"


@pytest.mark.parametrize("case", ["named_restored", "only_unnamed_changed", "named_mismatch"])
def test_named_fst_verification_preserves_raw_unnamed_evidence_without_using_it_as_witness(
    controller, case
):
    module, host = controller
    host.effects[2][1] = "ExistingFX"
    names = ["Param 0", "Param 1", ""]
    expected = [0.25, 0.75, 0.0]
    before = [0.25, 0.75, 0.0] if case == "only_unnamed_changed" else [0.9, 0.75, 0.0]
    host.values["ExistingFX"] = before
    state = begin(
        module,
        replace_existing=True,
        expected_loaded_plugin="ExistingFX",
        expected_names=names,
        expected_values=expected,
    )
    ready(module, host)
    dispatch(module, "confirm", operation_id=state["operation_id"])
    dispatch(module, "acknowledgeDrop", operation_id=state["operation_id"])
    host.values["ExistingFX"] = [0.4 if case == "named_mismatch" else 0.25, 0.75, 0.5]
    for _ in range(4):
        module.on_idle()
    if case == "named_restored":
        result = dispatch(module, "status", operation_id=state["operation_id"])
        assert result["status"] == "loaded"
        assert result["verification"] == "named_published_parameters"
        assert result["unnamed_parameter_differences"] == [
            {"index": 2, "saved": 0.0, "actual": 0.5}
        ]
        assert (
            result["unnamed_state_verified"] is False and result["hidden_state_verified"] is False
        )
        assert result["result"]["parameter_values"][2] == 0.5
        assert result["result"]["parameter_names"] == names
    else:
        assert module._operation["status"] == "loading"
        module._operation["_deadline"] = time.monotonic() - 1
        module.on_idle()
        assert module._operation["status"] == "unknown"
        if case == "only_unnamed_changed":
            assert module._operation["observable_no_op"] is True
            assert module._operation["verification"] == "no_observable_application_witness"


def test_malformed_named_manifest_is_rejected_before_ui(controller):
    module, host = controller
    with pytest.raises(ValueError, match="names must match"):
        begin(module, expected_names=["Room"], expected_values=[0.0, 0.5])
    assert host.actions == []


@pytest.mark.parametrize(
    "after_name,preserved,write_count",
    [("state", True, 1), ("OldSynth", True, 0), ("User lead", False, 0)],
)
def test_fst_restores_only_its_private_channel_label_and_keeps_third_party_name(
    controller, monkeypatch, after_name, preserved, write_count
):
    module, host = controller
    values = [0.9, 0.75]
    writes = []
    monkeypatch.setattr(
        module.plugins,
        "getPluginName",
        lambda index, slot, user_name, global_index: (
            host.channels[index] if user_name else "OldSynth"
        ),
    )
    monkeypatch.setattr(module.plugins, "getParamCount", lambda *args: 2)
    monkeypatch.setattr(
        module.plugins,
        "getParamValue",
        lambda param, index, slot, use_global: (
            values[param] if index == 0 else host.parameters(index, slot)[param]
        ),
    )

    def rename(index, name, use_global):
        assert index == 0 and use_global is True
        writes.append(name)
        host.channels[index] = name

    monkeypatch.setattr(module.channels, "setChannelName", rename, raising=False)
    state = dispatch(
        module,
        "beginPreset",
        kind="generator",
        index=0,
        relative_id="MCP Presets/Generators/state.fst",
        replace_existing=True,
        expected_loaded_plugin="OldSynth",
        expected_values=[0.5, 0.75],
        expected_names=["Param 0", "Param 1"],
    )
    ready(module, host)
    dispatch(module, "confirm", operation_id=state["operation_id"])
    values[0] = 0.5
    host.channels[0] = after_name
    dispatch(module, "acknowledgeDrop", operation_id=state["operation_id"])
    for _ in range(4):
        module.on_idle()
    result = dispatch(module, "status", operation_id=state["operation_id"])
    assert result["status"] == "loaded" and result["channel_name_preserved"] is preserved
    assert len(writes) == write_count
    assert host.channels[0] == ("OldSynth" if preserved else "User lead")
    assert result["before"]["parameter_values"] == [0.9, 0.75]
    assert result["result"]["parameter_values"] == [0.5, 0.75]
    assert result["before"]["parameter_names"] == result["result"]["parameter_names"]


def test_registered_fx_fst_save_uses_verified_slot_menu_and_shared_native_saveas(
    controller, monkeypatch, tmp_path
):
    """Exercise production dispatch/menu/dialog/file seams without an FL window."""
    from PIL import Image, ImageGrab

    module, host = controller
    destination = tmp_path / "Effect full state.fst"
    payload = fst_bytes(b"private native effect state\x00\xff")
    phase = ["closed"]
    selected_filename = [""]
    events = []

    class ControllerConnection:
        def transaction(self):
            return contextlib.nullcontext()

        def send_command(self, command, params):
            events.append(command)
            try:
                return {"success": True, **module.dispatch(command, params)}
            except Exception as exc:
                return {"success": False, "error": str(exc)}

    def control_id(hwnd):
        return {1000: 1001, 1200: 1, 1201: 2}[hwnd]

    class Windows(QuietWindows):
        user = types.SimpleNamespace(GetDlgCtrlID=control_id)

        def windows(self, class_name):
            assert class_name == "TQuickPopupMenuWindow"
            return [777] if phase[0] == "menu" else []

        def children(self, parent=None, class_name=None):
            assert parent == 888
            return [1000] if class_name == "Edit" else [1200, 1201]

        def wait_window(self, class_name, timeout):
            if class_name == "TQuickPopupMenuWindow":
                assert phase[0] == "menu"
                return 777
            assert class_name == "#32770" and phase[0] == "dialog"
            return 888

        def rect(self, hwnd):
            return (20, 30, 80, 60) if hwnd == 1200 else (0, 0, 236, 511)

        def _check_point(self, hwnd, point):
            assert hwnd in (1, 777)

        def click(self, point):
            if phase[0] == "closed":
                assert point == (9, 30)
                phase[0] = "menu"
                events.append("verified effect slot arrow")
            elif phase[0] == "menu":
                assert point == (48, 151)
                phase[0] = "dialog"
                events.append("shared plugin Save preset row")
            else:
                assert point == (50, 45) and selected_filename[0]
                Path(selected_filename[0]).write_bytes(payload)
                phase[0] = "saved"
                events.append("localized SaveAs ID1")

        def set_text(self, hwnd, value):
            assert hwnd == 1000
            selected_filename[0] = value

        def text(self, hwnd):
            assert hwnd == 1000
            return selected_filename[0]

    async def actual_fx_menu_rows(image):
        # Exact labels/bounds read from the saved real Correlometer menu capture.
        return [
            {"text": "Replace", "box": (20, 7, 58, 20)},
            {"text": "Browse presets", "box": (20, 126, 92, 138)},
            {"text": "Save preset", "box": (20, 145, 75, 157)},
        ]

    monkeypatch.setattr(tools, "get_connection", ControllerConnection)
    monkeypatch.setattr(native, "FLWindows", Windows)
    monkeypatch.setattr(
        native,
        "_slot_layout",
        lambda windows, slots, slot: {
            "window": 1,
            "crop": (0, 0, 200, 400),
            "row": {"box": (20, 20, 100, 40)},
            "y": 30,
        },
    )
    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: Image.new("RGB", (236, 511)))
    monkeypatch.setattr(native, "_ocr_lines", actual_fx_menu_rows)
    monkeypatch.setattr(
        native, "_editor_menu", lambda *args: pytest.fail("FX assumed generator wrapper")
    )
    registry = management_registry()
    result = registry["fl_save_plugin_preset"](
        str(destination), "effect", 2, slot_index=0, expected_plugin="NeighborFX"
    )
    assert result["success"] and result["status"] == "saved"
    assert destination.read_bytes() == payload
    manifest = json.loads(destination.with_suffix(".fst.mcp.json").read_text(encoding="utf-8"))
    assert manifest["parameter_names"] == ["Param 0", "Param 1"]
    assert manifest["parameter_values"] == [0.125, 0.75]
    assert manifest["parameters_complete"]
    assert events.index("pluginManagement.assertTarget") < events.index(
        "verified effect slot arrow"
    )
    assert events.index("shared plugin Save preset row") < events.index("localized SaveAs ID1")
    assert host.effects[2][0] == "NeighborFX" and host.actions


@pytest.mark.parametrize(
    "label",
    [
        "CON",
        "con.patch",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "LPT9",
        "COM¹",
        "CONIN$",
        "trail.",
        "trail ",
        "bad:name",
        "bad/name",
        "bad\\name",
        "bad?name",
        "bad*name",
        "bad<name",
        "bad|name",
        "bad\x01name",
        ".",
        "..",
        "a" * 252,
    ],
)
def test_invalid_display_leaf_is_rejected_without_midi_or_refresh(tmp_path, monkeypatch, label):
    source = tmp_path / "safe-source.fst"
    source.write_bytes(fst_bytes())
    info = native.validate_fst(source)
    source.with_suffix(".fst.mcp.json").write_text(
        json.dumps({"schema": 1, "sha256": info["sha256"], "display_name": label}), encoding="utf-8"
    )
    conn = Connection(lambda *args: pytest.fail("Invalid label reached MIDI"))
    monkeypatch.setattr(native, "FLWindows", lambda: pytest.fail("Invalid label reached UI"))
    with pytest.raises(ValueError, match="legal Windows basename"):
        native.load_preset(conn, TARGET, str(source), tmp_path)
    assert conn.commands == [] and not (tmp_path / "Presets").exists()


def test_same_display_leaf_is_isolated_in_distinct_unknown_directories(tmp_path, monkeypatch):
    source = tmp_path / "external-preset.fst"
    source.write_bytes(fst_bytes())
    info = native.validate_fst(source)
    source.with_suffix(".fst.mcp.json").write_text(
        json.dumps({"schema": 1, "sha256": info["sha256"], "display_name": "My Effect"}),
        encoding="utf-8",
    )
    sequence = [0]

    def respond(command, params):
        if command.endswith("snapshot"):
            return snapshot(occupied=False)
        assert command.endswith("beginPreset")
        sequence[0] += 1
        assert params["relative_id"].split("/")[-1] == "My Effect.fst"
        assert len(params["relative_id"].split("/")) == 4
        return {
            "success": True,
            "operation_id": "isolated-" + str(sequence[0]),
            "status": "loading",
        }

    monkeypatch.setattr(native, "FLWindows", QuietWindows)
    monkeypatch.setattr(
        native,
        "_poll",
        lambda conn, op, timeout: {
            "success": False,
            "status": "unknown",
            "operation_id": op,
            "activation_issued": True,
        },
    )
    first = native.load_preset(Connection(respond), TARGET, str(source), tmp_path)
    second = native.load_preset(Connection(respond), TARGET, str(source), tmp_path)
    one, two = Path(first["staged_preset"]), Path(second["staged_preset"])
    assert one.name == two.name == "My Effect.fst" and one.parent != two.parent
    assert one.read_bytes() == two.read_bytes() == source.read_bytes()
    assert Path(first["staged_directory"]).is_dir() and Path(second["staged_directory"]).is_dir()


def test_success_removes_only_own_leaf_and_empty_staging_directory(tmp_path, monkeypatch):
    source = tmp_path / "Factory Preset.fst"
    source.write_bytes(fst_bytes())
    seen = []

    def respond(command, params):
        if command.endswith("snapshot"):
            return snapshot(occupied=False)
        seen.append(params["relative_id"])
        return {"success": True, "operation_id": "cleanup-folder", "status": "loading"}

    monkeypatch.setattr(native, "FLWindows", QuietWindows)
    monkeypatch.setattr(
        native,
        "_poll",
        lambda *args: {"success": True, "status": "loaded", "activation_issued": True},
    )
    result = native.load_preset(Connection(respond), TARGET, str(source), tmp_path)
    staged = tmp_path / "Presets/Plugin database" / seen[0]
    assert staged.name == "Factory Preset.fst"
    assert not staged.exists() and not staged.parent.exists()
    assert result["staged_preset"] is None and result["staged_directory"] is None
    cached = native.preset_operations.get("cleanup-folder")
    assert cached["staged_preset"] is None and cached["staged_directory"] is None
    assert cached["status"] == "loaded" and cached["runner_active"] is False
    assert source.read_bytes() == fst_bytes()


def test_cleanup_never_recursively_deletes_other_files_in_private_folder(tmp_path, monkeypatch):
    source = tmp_path / "Preset.fst"
    source.write_bytes(fst_bytes())
    other = []

    def respond(command, params):
        if command.endswith("snapshot"):
            return snapshot(occupied=False)
        staged = tmp_path / "Presets/Plugin database" / params["relative_id"]
        foreign = staged.parent / "native-cache.nfo"
        foreign.write_text("keep", encoding="utf-8")
        other.append(foreign)
        return {"success": True, "operation_id": "nonempty-folder", "status": "loading"}

    monkeypatch.setattr(native, "FLWindows", QuietWindows)
    monkeypatch.setattr(
        native,
        "_poll",
        lambda *args: {"success": True, "status": "loaded", "activation_issued": True},
    )
    result = native.load_preset(Connection(respond), TARGET, str(source), tmp_path)
    assert result["status"] == "loaded" and result["staged_preset"] is None
    assert "staging_cleanup_error" in result and Path(result["staged_directory"]).is_dir()
    assert other[0].read_text(encoding="utf-8") == "keep"
    assert not other[0].with_name("Preset.fst").exists()
    cached = native.preset_operations.get("nonempty-folder")
    assert cached["staging_cleanup_error"] == result["staging_cleanup_error"]
    assert cached["staged_directory"] == result["staged_directory"]
    assert cached["staged_preset"] is None


def test_controller_traverses_exact_private_folder_then_normal_leaf(controller):
    module, old_host = controller
    nested = old_host.database / "MCP Presets/Effects/MCP amber delta forest/Effect.fst"
    nested.parent.mkdir()
    nested.write_bytes(fst_bytes())
    foreign = old_host.database / "MCP Presets/Effects/User Folder/Effect.fst"
    foreign.parent.mkdir()
    foreign.write_bytes(fst_bytes())
    wrong_kind = old_host.database / "MCP Presets/Generators/MCP amber delta forest/Effect.fst"
    wrong_kind.parent.mkdir()
    wrong_kind.write_bytes(fst_bytes())
    for relative in (
        "MCP Presets/Effects/User Folder/Effect.fst",
        "MCP Presets/Generators/MCP amber delta forest/Effect.fst",
    ):
        with pytest.raises(ValueError, match="Invalid staged preset identity"):
            begin(module, relative_id=relative)
    assert old_host.actions == []
    refreshed = Host(old_host.database)
    old_host.children, old_host.types = refreshed.children, refreshed.types
    result = begin(module, relative_id="MCP Presets/Effects/MCP amber delta forest/Effect.fst")
    assert ready(module, old_host)["stage"] == "awaiting_drag"
    assert old_host.selected.endswith("MCP amber delta forest/Effect.fst")
    assert result["preset_name"] == "Effect"


def test_source_leaf_validation_preserves_full_user_label_without_silent_rewrite():
    assert native._staging_leaf_name(Path("External Factory.fst"), None) == "External Factory.fst"
    assert (
        native._staging_leaf_name(Path("state.fst"), {"display_name": "EQ 1 - Mix"})
        == "EQ 1 - Mix.fst"
    )
    assert (
        native._staging_leaf_name(Path("state.fst"), {"display_name": "Tone.fst"}) == "Tone.fst.fst"
    )


def test_slot_text_roi_excludes_knob_without_stripping_real_name_suffix(monkeypatch):
    from PIL import Image, ImageGrab

    label = "Plugin O"
    slots = [{"occupied": False, "plugin_name": None} for _ in range(10)]
    slots[3] = {"occupied": True, "plugin_name": label}

    class Windows:
        def children(self, **kwargs):
            return [1]

        def rect(self, hwnd):
            return (0, 0, 1000, 700)

    def capture(bbox, **kwargs):
        shade = 50 if bbox[0] == 0 else 200
        return Image.new("RGB", (bbox[2] - bbox[0], bbox[3] - bbox[1]), (shade,) * 3)

    async def rows(picture):
        if picture.getpixel((0, 0)) < 100:
            return []
        if picture.height < 100:
            assert picture.width <= 313  # Crop excludes pixel x313 where circular controls begin.
            return [{"text": label, "box": (159, 5, 300, 20)}]
        return [
            {"text": f"Slot {n + 1}", "box": (159, 70 + n * 35, 194, 83 + n * 35)}
            for n in range(10)
            if n != 3
        ] + [{"text": label + " O", "box": (159, 175, 355, 190)}]

    monkeypatch.setattr(ImageGrab, "grab", capture)
    monkeypatch.setattr(native, "_ocr_lines", rows)
    layout = native._slot_layout(Windows(), slots, 3)
    assert layout["row"]["text"] == "Plugin O"
    assert layout["pitch"] == 35 and layout["row"]["box"][2] == 300


def test_browser_refresh_waits_for_temporary_tree_rebuild(monkeypatch):
    selected = [""]
    lookups = []
    clicks = []

    class Connection:
        def send_command(self, command, params):
            return {
                "success": True,
                "focused": True,
                "form_id": 4,
                "browser_form_id": 4,
                "popup_open": False,
                "node_caption": selected[0],
            }

    class Windows(QuietWindows):
        def children(self, **kwargs):
            lookups.append(True)
            return [] if len(lookups) == 1 else [7]

        def click_text(self, hwnd, text):
            assert hwnd == 7 and text == "Current project"
            clicks.append(text)
            selected[0] = text

    monkeypatch.setattr(native.time, "sleep", lambda seconds: None)
    native._refresh_browser(Windows(), Connection())
    assert len(lookups) == 2 and clicks == ["Current project"]


def test_plugin_delete_confirmation_waits_for_exact_prompt_to_finish_drawing(monkeypatch):
    from PIL import Image, ImageGrab

    reads = []
    clicks = []

    class Windows(QuietWindows):
        def windows(self, **kwargs):
            return [1]

        def rect(self, hwnd):
            return (0, 0, 355, 207)

        def _check_point(self, hwnd, point):
            assert hwnd == 1

        def click(self, point):
            assert len(reads) >= 2
            clicks.append(point)

    async def rows(picture):
        reads.append(True)
        if len(reads) == 1:
            return [{"text": "Confirm", "box": (5, 5, 55, 20)}]
        return [
            {"text": "The selected plugin is about to be deleted.", "box": (95, 48, 330, 62)},
            {"text": "0k", "box": (234, 173, 248, 183)},
        ]

    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: Image.new("RGB", (355, 207)))
    monkeypatch.setattr(native, "_ocr_lines", rows)
    monkeypatch.setattr(native.time, "sleep", lambda seconds: None)
    assert native._confirm_plugin_delete(Windows(), "effect") is True
    assert clicks == [(241, 178)]


def test_leaf_creation_collision_never_removes_external_winner(tmp_path, monkeypatch):
    source = tmp_path / "Source.fst"
    source.write_bytes(fst_bytes())
    original_open = Path.open
    foreign = []

    def raced_open(path, mode="r", *args, **kwargs):
        if mode == "xb":
            with original_open(path, "wb") as output:
                output.write(b"external winner")
            foreign.append(path)
            raise FileExistsError("Another owner created this leaf")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", raced_open)
    monkeypatch.setattr(native, "FLWindows", lambda: pytest.fail("Leaf race reached UI"))
    conn = Connection(lambda *args: snapshot(occupied=False))
    with pytest.raises(FileExistsError, match="Another owner"):
        native.load_preset(conn, TARGET, str(source), tmp_path)
    assert len(foreign) == 1 and foreign[0].read_bytes() == b"external winner"
    assert foreign[0].parent.is_dir()
    assert all(
        not command.endswith(("beginPreset", "prepareBrowser")) for command, params in conn.commands
    )


def test_browser_tab_selection_precedes_refresh_seed_and_is_not_repeated_by_fsm(
    controller, monkeypatch
):
    module, host = controller
    selections = []
    host.types[""] = -1

    def select_tab(direction):
        assert direction == 0
        selections.append(direction)
        host.selected = ""  # Real tab changes clear the Browser selected node.

    monkeypatch.setattr(module.ui, "navigateBrowserTabs", select_tab)
    assert dispatch(module, "prepareBrowser")["node_caption"] == ""
    host.selected = "Current project"  # Native refresh confirmed its visible root.
    begin(module)
    assert ready(module, host)["stage"] == "awaiting_drag"
    assert selections == [0]


def test_browser_refresh_reseeds_even_if_old_caption_survives_initial_reread(monkeypatch):
    selected = ["Old preset.fst"]
    clicks = []
    confirmations = []

    class Connection:
        def send_command(self, command, params):
            if selected[0] == "Current project":
                confirmations.append(True)
            return {
                "success": True,
                "focused": True,
                "form_id": 4,
                "browser_form_id": 4,
                "popup_open": False,
                "node_caption": selected[0],
            }

    class Windows(QuietWindows):
        def click_text(self, hwnd, text):
            assert hwnd == 7 and text == "Current project"
            clicks.append(text)
            selected[0] = text

    monkeypatch.setattr(native.time, "sleep", lambda seconds: None)
    native._refresh_browser(Windows(), Connection())
    assert clicks == ["Current project"] and len(confirmations) == 2


@pytest.mark.parametrize(
    "outcome", ["correct", "wrong_same_plugin", "route_changed", "enabled_changed"]
)
def test_generator_delete_verifies_surviving_channel_identity_beyond_same_plugin_name(
    monkeypatch, outcome
):
    target = {
        "kind": "generator", "index": 1, "slot_index": -1,
        "expected_plugin": "Speldosa",
    }
    previous = [
        {
            "kind": "generator", "index": index, "slot_index": -1,
            "occupied": True, "plugin_name": "Speldosa",
            "display_name": name, "channel_name": name,
            "mixer_track": index + 4, "enabled": index != 2,
        }
        for index, name in enumerate(("Original A", "Scratch instance", "Original B"))
    ]
    before = dict(
        previous[target["index"]], success=True, parameter_count=2,
        parameter_values=[0.0, 0.75], parameter_names=["Room", "Gain"],
        parameter_fingerprint="all-state", named_parameter_fingerprint="named-state",
        parameters_complete=True,
    )
    current = [dict(item) for item in previous]
    events = []
    inventories = []

    def respond(command, params):
        if command == "pluginManagement.snapshot":
            assert params == dict(target, parameter_limit=16384, include_names=True)
            events.append("snapshot")
            return dict(before)
        if command == "pluginManagement.inventory":
            assert params == {}
            inventories.append([dict(item) for item in current])
            return {"success": True, "instruments": [dict(item) for item in current]}
        assert command == "pluginManagement.prepareTarget" and params == target
        events.append("prepare_target")
        return {"success": True}

    conn = Connection(respond)

    def editor_menu(windows, actual_conn, actual_target, actual_before):
        assert actual_conn is conn and actual_target == target and actual_before == before
        assert actual_before["channel_name"] == "Scratch instance"
        assert actual_before["parameter_names"] == ["Room", "Gain"]
        events.append("verified_editor")

    def delete_menu(windows):
        assert events[-1] == "verified_editor"
        events.append("delete_menu")
        del current[2 if outcome == "wrong_same_plugin" else target["index"]]
        for index, item in enumerate(current):
            item["index"] = index
        if outcome == "route_changed":
            current[0]["mixer_track"] = 99
        elif outcome == "enabled_changed":
            current[0]["enabled"] = not current[0]["enabled"]

    def confirm_delete(windows, kind):
        assert kind == "generator"
        assert events[-1] == "delete_menu"
        events.append("confirmed")
        return True

    monkeypatch.setattr(native, "FLWindows", QuietWindows)
    monkeypatch.setattr(native, "_editor_menu", editor_menu)
    monkeypatch.setattr(native, "_choose_delete", delete_menu)
    monkeypatch.setattr(native, "_choose", lambda *args: pytest.fail("Legacy exact Delete chooser"))
    monkeypatch.setattr(native, "_confirm_plugin_delete", confirm_delete)
    if outcome == "correct":
        result = native.delete_plugin(conn, target)
        assert result["status"] == "deleted" and result["index"] == 1
        assert current[1]["index"] == 1 and current[1]["channel_name"] == "Original B"
    else:
        with pytest.raises(RuntimeError, match="Unexpected Channel Rack changes"):
            native.delete_plugin(conn, target)
    assert events == ["snapshot", "prepare_target", "verified_editor", "delete_menu", "confirmed"]
    assert len(inventories) == 2 and inventories[0] == previous
    assert [item["plugin_name"] for item in inventories[1]] == ["Speldosa", "Speldosa"]
