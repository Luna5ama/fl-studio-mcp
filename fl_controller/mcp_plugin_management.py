"""Plugin inventory, observable fingerprints and explicit preset destinations.

Runs inside FL. GUI state machines advance only through idle/status callbacks.
"""

import hashlib
import json
import math
import time
from pathlib import Path

import channels
import midi
import mixer
import plugins
import transport
import ui

_script_dir = None
_operation = None
_steps = None
_advancing = False
_FULL_STATE_PARAMETER_LIMIT = 16384


def configure(script_dir):
    global _script_dir
    _script_dir = Path(script_dir)


def _require_stopped():
    if transport.isPlaying() or transport.isRecording():
        raise RuntimeError("Stop playback and recording before loading a preset")


def _focused_generator_index():
    # FL's focused generator form ID is local to the current Rack group. This
    # differs from the global indices used by the plugin-management protocol.
    form_id = ui.getFocusedFormID()
    if not ui.getFocused(midi.widPluginGenerator) or not 0 <= form_id < channels.channelCount(
        False
    ):
        return None
    return channels.getChannelIndex(form_id)


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(name + " is out of range")
    return value


def _target(params, require_plugin=True):
    kind = params.get("kind")
    if kind == "generator":
        index = _integer(params.get("index"), "channel index", 0, channels.channelCount(True) - 1)
        slot = -1
    elif kind == "effect":
        index = _integer(params.get("index"), "mixer track", 0, mixer.trackCount() - 2)
        slot = _integer(params.get("slot_index"), "effect slot", 0, 9)
    else:
        raise ValueError("kind must be generator or effect")
    valid = bool(plugins.isValid(index, slot, True))
    if require_plugin and not valid:
        raise ValueError("No plugin at the requested destination")
    expected = params.get("expected_plugin")
    name = plugins.getPluginName(index, slot, 0, True) if valid else None
    if expected is not None and name != expected:
        raise ValueError("Plugin identity changed; refresh inventory before retrying")
    return kind, index, slot, valid


def _snapshot(kind, index, slot, parameter_limit=128, include_names=False):
    valid = bool(plugins.isValid(index, slot, True))
    item = {
        "kind": kind,
        "index": index,
        "slot_index": slot,
        "occupied": valid,
        "plugin_name": None,
        "enabled": None,
        "enabled_source": "unavailable",
    }
    if kind == "generator":
        item["channel_name"] = channels.getChannelName(index, True)
        item["enabled"] = not bool(channels.isChannelMuted(index, True))
        item["enabled_source"] = "channel_mute"
        item["mixer_track"] = channels.getTargetFxTrack(index, True)
    if not valid:
        return item
    item["plugin_name"] = plugins.getPluginName(index, slot, 0, True)
    item["display_name"] = plugins.getPluginName(index, slot, 1, True)
    count = plugins.getParamCount(index, slot, True)
    values = [
        plugins.getParamValue(param, index, slot, True)
        for param in range(min(count, parameter_limit))
    ]
    item["parameter_count"] = count
    item["parameter_values"] = values
    item["parameter_fingerprint"] = hashlib.sha256(
        json.dumps(values, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    item["parameters_complete"] = count <= parameter_limit
    if include_names:
        names = [plugins.getParamName(param, index, slot, True) for param in range(len(values))]
        item["parameter_names"] = names
        named = [(param, name, values[param]) for param, name in enumerate(names) if name.strip()]
        item["named_parameter_count"] = len(named)
        item["named_parameter_fingerprint"] = hashlib.sha256(
            json.dumps(named, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    if kind == "effect":
        item["mix_level"] = (
            mixer.getPluginMixLevel(index, slot) if hasattr(mixer, "getPluginMixLevel") else None
        )
        item["track_effects_enabled"] = bool(mixer.isTrackSlotsEnabled(index))
    return item


def _identity(item):
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


def _track_snapshot(track):
    return [_snapshot("effect", track, slot) for slot in range(10)]


def _public(state):
    return {key: value for key, value in state.items() if not key.startswith("_")}


def is_busy():
    return _operation is not None and _operation["status"] == "loading"


def _cleanup():
    if _operation is not None and "_auto_hide" in _operation:
        ui.setBrowserAutoHide(_operation["_auto_hide"])
        del _operation["_auto_hide"]


def on_deinit():
    """Release temporary host preferences without undoing an issued activation."""
    global _steps
    if is_busy():
        issued = bool(_operation.get("_issued"))
        _operation.update(
            success=False,
            status="unknown" if issued else "cancelled",
            error="Controller stopped after activation; inspect FL before retrying"
            if issued
            else "Controller stopped before activation",
            inspect_before_retrying=issued,
            activation_may_have_occurred=issued,
        )
    _steps = None
    try:
        _cleanup()
    except Exception as exc:
        # FL may have already torn down its UI when the device is disconnected.
        # Keep the cleanup error visible for callers that can still query status.
        if _operation is not None:
            _operation["cleanup_error"] = str(exc)


def _move_browser(direction):
    for _ in range(5):
        ui.setFocused(midi.widBrowser)
        yield
        before = (ui.getFocusedNodeCaption(), ui.getFocusedNodeFileType())
        ui.navigateBrowser(direction, 0)
        yield
        if before != (ui.getFocusedNodeCaption(), ui.getFocusedNodeFileType()):
            return True
        if ui.getFocused(midi.widBrowser):
            return False
    raise RuntimeError("Browser focus changed repeatedly during preset loading")


def _preset_steps(parts):
    state = _operation
    ui.setBrowserAutoHide(0)
    ui.showWindow(midi.widBrowser)
    ui.setFocused(midi.widBrowser)
    # prepareBrowser selected ALL before native refresh seeded its root. Switching
    # tabs here can clear that selection again and make arrow navigation a no-op.
    yield
    for _ in range(4096):
        if ui.getFocusedNodeFileType() <= -100:
            ui.toggleBrowserNode(0)
            yield
        if not (yield from _move_browser(midi.FPT_Up)):
            break
    else:
        raise RuntimeError("Cannot reach Browser root; clear its search field")
    database = _script_dir.parents[2] / "Presets" / "Plugin database"
    nodes = ["Plugin database", *parts]
    for level, name in enumerate(nodes):
        allowed = None
        if level:
            parent = database.joinpath(*parts[: level - 1])
            allowed = {entry.name.casefold() for entry in parent.iterdir()}
        for _ in range(4096 if allowed is None else len(allowed) + 1):
            caption = ui.getFocusedNodeCaption()
            if allowed is not None and caption.casefold() not in allowed:
                raise RuntimeError("Browser left the expected preset folder")
            if caption.casefold() == name.casefold():
                break
            if ui.getFocusedNodeFileType() <= -100:
                ui.toggleBrowserNode(0)
                yield
            if not (yield from _move_browser(midi.FPT_Down)):
                raise RuntimeError(
                    "Preset Browser node not found: "
                    + name
                    + "; focused="
                    + repr(ui.getFocusedNodeCaption())
                    + "; type="
                    + str(ui.getFocusedNodeFileType())
                )
        else:
            raise RuntimeError("Preset Browser search exceeded its bounds")
        if level < len(nodes) - 1:
            if ui.getFocusedNodeFileType() > -100:
                raise RuntimeError("Expected a preset folder")
            ui.toggleBrowserNode(1)
            yield
            yield from _move_browser(midi.FPT_Down)
    if ui.getFocusedNodeFileType() != 4:
        raise RuntimeError("Selected Browser item is not an FST preset")
    if state["kind"] == "effect":
        ui.showWindow(midi.widMixer)
        ui.setFocused(midi.widMixer)
        yield
    else:
        ui.showWindow(midi.widChannelRack)
        ui.setFocused(midi.widChannelRack)
        yield
    ui.showWindow(midi.widBrowser)
    ui.setFocused(midi.widBrowser)
    yield
    if state["kind"] == "effect":
        mixer.setActiveTrack(state["index"])
        yield
    state["stage"] = "awaiting_drag"
    while not state.get("_drop_acknowledged"):
        yield
    yield
    while True:
        current = _snapshot(
            state["kind"], state["index"], state["slot_index"], _FULL_STATE_PARAMETER_LIMIT, True
        )
        if current["occupied"] and current["plugin_name"].strip().casefold() not in (
            "",
            "fruity wrapper",
            "(none)",
        ):
            before = state["_before"]
            witness = (
                "empty_destination_became_occupied"
                if not before["occupied"]
                else "plugin_identity_changed"
                if current["plugin_name"] != before["plugin_name"]
                else "published_parameters_changed"
                if (current["named_parameter_fingerprint"] != before["named_parameter_fingerprint"])
                else None
            )
            state["_application_witness"] = witness
            state["observable_no_op"] = witness is None
            if witness is None:
                yield
                continue
            expected = state.get("expected_loaded_plugin")
            if expected is not None and current["plugin_name"] != expected:
                yield
                continue
            if channels.channelCount(True) != state["_channel_count"]:
                raise RuntimeError("Unexpected Channel Rack change during preset replacement")
            if state["kind"] == "effect":
                after = _track_snapshot(state["index"])
                for slot in range(10):
                    if slot != state["slot_index"] and _identity(after[slot]) != _identity(
                        state["_track_before"][slot]
                    ):
                        raise RuntimeError("A neighboring effect changed during preset loading")
            else:
                for index, before in enumerate(state["_channels_before"]):
                    if (
                        index != state["index"]
                        and _identity(_snapshot("generator", index, -1)) != before
                    ):
                        raise RuntimeError("A neighboring instrument changed during preset loading")
            expected_values = state.get("_expected_values")
            expected_names = state.get("_expected_names")
            unnamed_differences = []
            if expected_values is not None:
                values = current["parameter_values"]
                if len(values) != len(expected_values):
                    yield
                    continue
                if expected_names is None:
                    matched = all(abs(a - b) <= 1e-5 for a, b in zip(values, expected_values))
                else:
                    expected_schema = [
                        (n, name) for n, name in enumerate(expected_names) if name.strip()
                    ]
                    actual_schema = [
                        (n, name)
                        for n, name in enumerate(current["parameter_names"])
                        if name.strip()
                    ]
                    matched = expected_schema == actual_schema and all(
                        abs(values[n] - expected_values[n]) <= 1e-5 for n, name in expected_schema
                    )
                    unnamed_differences = [
                        {"index": n, "saved": expected_values[n], "actual": values[n]}
                        for n, name in enumerate(expected_names)
                        if not name.strip() and abs(values[n] - expected_values[n]) > 1e-5
                    ]
                if not matched:
                    yield
                    continue
            channel_name_preserved = None
            channel_name_restoration = None
            if state["kind"] == "generator":
                original_name = state["_before"].get("channel_name")
                observed_name = current.get("channel_name")
                if observed_name == original_name:
                    channel_name_preserved = True
                    channel_name_restoration = "already_original"
                elif original_name is not None and observed_name == state["preset_name"]:
                    channels.setChannelName(state["index"], original_name, True)
                    current = _snapshot(
                        state["kind"],
                        state["index"],
                        state["slot_index"],
                        _FULL_STATE_PARAMETER_LIMIT,
                        True,
                    )
                    if current.get("channel_name") != original_name:
                        raise RuntimeError("FST applied but original channel name was not restored")
                    channel_name_preserved = True
                    channel_name_restoration = "restored_private_staging_name"
                else:
                    # A user or the source preset supplied another name. Do not
                    # overwrite it merely because it differs from our snapshot.
                    channel_name_preserved = False
                    channel_name_restoration = "third_name_retained"
            # For presets created here, parameter readback is an independent witness.
            # External FST files can only verify host-visible destination/identity.
            state.update(
                status="loaded",
                stage="complete",
                inspect_before_retrying=False,
                before=state["_before"],
                result=current,
                channel_name_preserved=channel_name_preserved,
                channel_name_restoration=channel_name_restoration,
                application_witness=witness,
                observable_no_op=False,
                hidden_state_independently_verified=False,
                hidden_state_verified=False,
                unnamed_state_verified=False,
                unnamed_parameter_differences=unnamed_differences,
                named_parameter_count=current["named_parameter_count"],
                verification="named_published_parameters"
                if expected_names is not None
                else "published_parameters"
                if expected_values is not None
                else "destination_and_plugin_identity",
            )
            return
        yield


def on_idle():
    global _steps, _advancing
    if _advancing or not is_busy():
        return
    _advancing = True
    try:
        if transport.isPlaying() or transport.isRecording():
            raise RuntimeError(
                "Playback or recording started during preset loading; application is unverified"
            )
        if time.monotonic() >= _operation["_deadline"]:
            if _operation.get("_drop_acknowledged") and not _operation.get("_application_witness"):
                _operation.update(
                    observable_no_op=True,
                    verification="no_observable_application_witness",
                    hidden_state_independently_verified=False,
                )
                raise RuntimeError(
                    "Preset drop completed without an observable application witness; "
                    "hidden state may have changed, inspect FL before retrying"
                )
            if _operation.get("_issued") and not _operation.get("_drop_acknowledged"):
                _operation["verification"] = "native_drop_not_acknowledged"
            raise RuntimeError("Preset loading timed out; inspect the destination before retrying")
        next(_steps)
    except StopIteration:
        _steps = None
        _cleanup()
    except Exception as exc:
        _operation.update(
            success=False,
            status="unknown" if _operation.get("_issued") else "failed",
            error=str(exc),
            inspect_before_retrying=True,
            activation_may_have_occurred=bool(_operation.get("_issued")),
        )
        _steps = None
        _cleanup()
    finally:
        _advancing = False


def dispatch(action, params):
    global _operation, _steps
    if not action.startswith("pluginManagement."):
        return None
    command = action.split(".", 1)[1]
    if command == "status":
        if _operation is None or params.get("operation_id") != _operation["operation_id"]:
            raise ValueError("Unknown preset loading operation")
        on_idle()
        return _public(_operation)
    if command == "cancel":
        if _operation is None or params.get("operation_id") != _operation["operation_id"]:
            raise ValueError("Unknown preset loading operation")
        if is_busy():
            if _operation.get("_issued"):
                _operation.update(
                    cancellation_requested=True,
                    cancellation_detail="Activation already issued; observing until the deadline",
                    inspect_before_retrying=True,
                )
            else:
                _operation.update(
                    success=False, status="cancelled", error="Cancelled before activation"
                )
                _cleanup()
        return _public(_operation)
    if command == "confirm":
        if not is_busy() or params.get("operation_id") != _operation["operation_id"]:
            raise ValueError("Unknown active preset loading operation")
        if _operation["stage"] != "awaiting_drag":
            raise RuntimeError("Preset destination is not ready")
        _require_stopped()
        kind, index, slot, _ = _target(_operation, require_plugin=False)
        now = _snapshot(kind, index, slot, _FULL_STATE_PARAMETER_LIMIT, True)
        if _identity(now) != _identity(_operation["_before"]):
            raise RuntimeError("Preset destination changed before activation")
        if kind == "effect" and mixer.trackNumber() != index:
            raise RuntimeError("Selected Mixer track changed")
        _operation["_issued"] = True
        _operation["activation_issued"] = True
        _operation["stage"] = "awaiting_drop"
        return {"status": "ready"}
    if command == "acknowledgeDrop":
        if not is_busy() or params.get("operation_id") != _operation["operation_id"]:
            raise ValueError("Unknown active preset loading operation")
        if not _operation.get("_issued") or _operation["stage"] != "awaiting_drop":
            raise RuntimeError("Preset activation was not confirmed for a native drop")
        _operation.update(_drop_acknowledged=True, drop_acknowledged=True, stage="verify")
        return {"status": "loading", "drop_acknowledged": True}
    if is_busy() and command not in ("trackSlots", "snapshot", "assertTarget"):
        raise RuntimeError("A preset load is active; wait for its status before another action")
    if command in ("prepareBrowser", "getBrowserState"):
        if command == "prepareBrowser":
            if ui.isInPopupMenu():
                raise RuntimeError("Close the open FL menu before preparing the Browser")
            ui.showWindow(midi.widBrowser)
            ui.setFocused(midi.widBrowser)
            ui.navigateBrowserTabs(0)
        return {
            "focused": bool(ui.getFocused(midi.widBrowser)),
            "form_id": ui.getFocusedFormID(),
            "browser_form_id": midi.widBrowser,
            "popup_open": bool(ui.isInPopupMenu()),
            "node_caption": ui.getFocusedNodeCaption(),
            "node_type": ui.getFocusedNodeFileType(),
        }
    if command == "inventory":
        include_empty = bool(params.get("include_empty", False))
        instruments = [
            _snapshot("generator", index, -1, 0) for index in range(channels.channelCount(True))
        ]
        effects = []
        for track in range(mixer.trackCount() - 1):
            slots = [_snapshot("effect", track, slot, 0) for slot in range(10)]
            if include_empty or any(item["occupied"] for item in slots):
                effects.append(
                    {
                        "mixer_track": track,
                        "name": mixer.getTrackName(track),
                        "slots": slots if include_empty else [s for s in slots if s["occupied"]],
                    }
                )
        return {
            "instruments": instruments,
            "mixer_tracks": effects,
            "individual_effect_enabled_available": False,
        }
    if command == "trackSlots":
        index = _integer(params.get("index"), "mixer track", 0, mixer.trackCount() - 2)
        return {"index": index, "slots": _track_snapshot(index)}
    if command == "assertTarget":
        kind, index, slot, _ = _target(params, require_plugin=False)
        if kind == "effect" and mixer.trackNumber() != index:
            raise RuntimeError("Selected Mixer track changed")
        if kind == "generator" and channels.channelNumber(True) != index:
            raise RuntimeError("Selected instrument changed")
        return _snapshot(kind, index, slot, _FULL_STATE_PARAMETER_LIMIT, True)
    if command == "editorState":
        form_id = ui.getFocusedFormID()
        focused = bool(ui.getFocused(midi.widPluginGenerator))
        local_count = channels.channelCount(False)
        mapped = (
            channels.getChannelIndex(form_id) if focused and 0 <= form_id < local_count else None
        )
        return {
            "focused_generator": focused,
            "focused_plugin_name": ui.getFocusedPluginName(),
            "form_id": form_id,
            "mapped_global_index": mapped,
            "channel_local": channels.channelNumber(False),
            "channel_global": channels.channelNumber(True),
            "count_local": local_count,
            "count_global": channels.channelCount(True),
        }
    if command == "assertEditor":
        kind, index, slot, _ = _target(params)
        if kind == "effect":
            active = mixer.getActiveEffectIndex()
            if active is None or tuple(active) != (index, slot):
                raise RuntimeError("The requested effect editor is not focused yet")
        elif (
            channels.channelNumber(True) != index
            or not ui.getFocused(midi.widPluginGenerator)
            or _focused_generator_index() != index
            or ui.getFocusedPluginName() != plugins.getPluginName(index, -1, 0, True)
        ):
            raise RuntimeError("The requested instrument editor is not focused yet")
        return {"status": "editor_verified"}
    if command == "snapshot":
        kind, index, slot, _ = _target(params, require_plugin=False)
        limit = _integer(
            params.get("parameter_limit", _FULL_STATE_PARAMETER_LIMIT), "parameter limit", 0, 16384
        )
        return _snapshot(kind, index, slot, limit, bool(params.get("include_names", False)))
    if command == "setMix":
        kind, index, slot, _ = _target(params)
        value = params.get("value")
        if (
            kind != "effect"
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not (math.isfinite(value) and 0 <= value <= 1)
        ):
            raise ValueError("Effect mix must be a finite value between 0 and 1")
        if not hasattr(mixer, "setPluginMixLevel"):
            raise RuntimeError("Per-plugin mix control requires MIDI API 37+")
        mixer.setPluginMixLevel(index, slot, value)
        actual = mixer.getPluginMixLevel(index, slot)
        if abs(actual - value) > 1e-5:
            raise RuntimeError("FL Studio did not apply the requested mix level")
        return {"kind": kind, "index": index, "slot_index": slot, "mix_level": actual}
    if command == "setInstrumentEnabled":
        kind, index, slot, _ = _target(params)
        if kind != "generator" or type(params.get("enabled")) is not bool:
            raise ValueError("An instrument and boolean enabled value are required")
        channels.muteChannel(index, 0 if params["enabled"] else 1, True)
        actual = not bool(channels.isChannelMuted(index, True))
        if actual != params["enabled"]:
            raise RuntimeError("Instrument enabled state did not change as requested")
        return {"enabled": actual, "enabled_source": "channel_mute"}
    if command in ("prepareTarget", "openEditor"):
        kind, index, slot, _ = _target(params)
        if ui.isInPopupMenu():
            raise RuntimeError("Close the open FL menu before selecting a plugin")
        if kind == "effect":
            ui.showWindow(midi.widMixer)
            ui.setFocused(midi.widMixer)
            mixer.setActiveTrack(index)
            if command == "openEditor":
                mixer.focusEditor(index, slot)
        else:
            channels.selectOneChannel(index, True)
            ui.showWindow(midi.widChannelRack)
            ui.setFocused(midi.widChannelRack)
            if command == "openEditor":
                channels.showEditor(index, 1, True)
                channels.focusEditor(index, True)
        return _snapshot(kind, index, slot)
    if command == "beginPreset":
        _require_stopped()
        kind, index, slot, occupied = _target(params, require_plugin=False)
        if occupied and not params.get("replace_existing", False):
            raise ValueError("Replacing an occupied plugin requires replace_existing=True")
        relative = params.get("relative_id", "")
        parts = relative.split("/") if isinstance(relative, str) else []
        if (
            len(parts) not in (3, 4)
            or parts[0] != "MCP Presets"
            or parts[1] != ("Effects" if kind == "effect" else "Generators")
            or (
                len(parts) == 4
                and (
                    not parts[2].startswith("MCP ")
                    or len(parts[2]) <= 4
                    or parts[2] != parts[2].strip()
                )
            )
            or any(part in ("", ".", "..") or ":" in part or "\\" in part for part in parts)
        ):
            raise ValueError("Invalid staged preset identity")
        if _script_dir is None:
            raise RuntimeError("Plugin-management module was not configured")
        database = _script_dir.parents[2] / "Presets" / "Plugin database"
        preset = database.joinpath(*parts).resolve()
        if (
            not preset.is_relative_to(database.resolve())
            or not preset.is_file()
            or preset.suffix != ".fst"
        ):
            raise ValueError("The staged preset is missing")
        timeout = params.get("timeout", 60)
        if not isinstance(timeout, (int, float)) or not 5 <= timeout <= 180:
            raise ValueError("timeout must be between 5 and 180 seconds")
        expected_values = params.get("expected_values")
        expected_names = params.get("expected_names")
        if expected_names is not None and (
            not isinstance(expected_names, list)
            or not isinstance(expected_values, list)
            or len(expected_names) != len(expected_values)
            or not all(isinstance(name, str) for name in expected_names)
        ):
            raise ValueError("Preset parameter names must match the complete value array")
        if ui.isInPopupMenu():
            raise RuntimeError("Close the open FL menu before loading a preset")
        _operation = {
            "success": True,
            "operation_id": str(time.monotonic_ns()),
            "status": "loading",
            "stage": "browser",
            "activation_issued": False,
            "drop_acknowledged": False,
            "kind": kind,
            "index": index,
            "slot_index": slot,
            "preset_name": preset.stem,
            "expected_loaded_plugin": params.get("expected_loaded_plugin"),
            "_expected_values": params.get("expected_values"),
            "_expected_names": params.get("expected_names"),
            "_before": _snapshot(kind, index, slot, _FULL_STATE_PARAMETER_LIMIT, True),
            "_channel_count": channels.channelCount(True),
            "_auto_hide": ui.isBrowserAutoHide(),
            "_deadline": time.monotonic() + timeout,
        }
        if kind == "effect":
            _operation["_track_before"] = _track_snapshot(index)
        else:
            _operation["_channels_before"] = [
                _identity(_snapshot("generator", n, -1)) for n in range(channels.channelCount(True))
            ]
        _steps = _preset_steps(parts)
        return _public(_operation)
    raise ValueError("Unknown plugin-management command: " + command)
