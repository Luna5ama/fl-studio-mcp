"""MIDI-side readback and focus preparation for native project file workflows."""

import channels
import general
import midi
import mixer
import transport
import ui


def _index(value, count, label):
    if type(value) is not int or not 0 <= value < count:
        raise ValueError("Invalid " + label)
    return value


def _channel(index):
    _index(index, channels.channelCount(True), "channel index")
    return {
        "index": index,
        "name": channels.getChannelName(index, True),
        "channel_type": channels.getChannelType(index, True),
        "mixer_track": channels.getTargetFxTrack(index, True),
    }


def _snapshot():
    return [
        {
            **{key: value for key, value in _channel(index).items() if key != "index"},
            "event_id": channels.getRecEventId(index, True),
        }
        for index in range(channels.channelCount(True))
    ]


def _verify_destination(index, before):
    if not isinstance(before, list):
        raise ValueError("Expected the original Channel Rack snapshot")
    after = _snapshot()
    if after[:index] + after[index + 1 :] != before:
        raise RuntimeError("Existing Channel Rack channels changed during sample import")


def dispatch(action, params):
    if not action.startswith("projectFiles."):
        return None
    if action == "projectFiles.status":
        return {
            "changed": general.getChangedFlag(),
            "playing": bool(transport.isPlaying()),
            "channel_count": channels.channelCount(True),
            "selected_channel": channels.channelNumber(True),
            "playlist_focused": bool(ui.getFocused(midi.widPlaylist)),
            "rack_focused": bool(ui.getFocused(midi.widChannelRack)),
            "popup_open": bool(ui.isInPopupMenu()),
        }
    if action == "projectFiles.prepare":
        if hasattr(general, "safeToEdit") and not general.safeToEdit():
            raise RuntimeError("Close FL Studio modal dialogs before this file operation")
        target = params.get("target", "playlist")
        if target not in ("playlist", "rack"):
            raise ValueError("target must be playlist or rack")
        if ui.isInPopupMenu():
            raise RuntimeError("Close the current FL Studio menu first")
        if params.get("stop_playback", False):
            transport.stop()
        window = midi.widPlaylist if target == "playlist" else midi.widChannelRack
        before = channels.channelCount(True)
        ui.showWindow(window)
        ui.setFocused(window)
        return {"channel_count": before, "target": target, "channels": _snapshot()}
    if action == "projectFiles.openRackMenu":
        if not ui.getFocused(midi.widChannelRack) or ui.isInPopupMenu():
            raise RuntimeError("Channel Rack is not ready for its menu")
        transport.globalTransport(midi.FPT_Menu, 1, midi.PME_System | midi.PME_FromMIDI)
        return {"status": "menu_requested"}
    if action == "projectFiles.inspectNewChannel":
        before = params.get("before_count")
        if type(before) is not int or before < 0:
            raise ValueError("before_count must be a nonnegative integer")
        count = channels.channelCount(True)
        if count != before + 1:
            return {
                "status": "waiting" if count == before else "unexpected_channel_change",
                "channel_count": count,
            }
        original = params.get("before_channels")
        if not isinstance(original, list) or len(original) != before:
            raise ValueError("Expected the original Channel Rack snapshot")
        current = _snapshot()
        candidates = [
            index
            for index in range(count)
            if current[:index] + current[index + 1 :] == original
            and current[index]["channel_type"] == params.get("channel_type")
        ]
        if len(candidates) != 1 or channels.channelNumber(True) != candidates[0]:
            raise RuntimeError("Cannot uniquely identify the newly created sample channel")
        index = candidates[0]
        return {"status": "created", "channel_count": count, "channel": _channel(index)}
    if action == "projectFiles.focusChannel":
        index = _index(params.get("index"), channels.channelCount(True), "channel index")
        _verify_destination(index, params.get("before_channels"))
        expected_type = params.get("channel_type")
        if channels.getChannelType(index, True) != expected_type:
            raise RuntimeError("New channel type does not match the requested sample destination")
        channels.selectOneChannel(index, True)
        ui.showWindow(midi.widChannelRack)
        ui.setFocused(midi.widChannelRack)
        return {"channel": _channel(index)}
    if action == "projectFiles.openChannelMenu":
        index = _index(params.get("index"), channels.channelCount(True), "channel index")
        _verify_destination(index, params.get("before_channels"))
        if channels.channelNumber(True) != index or not ui.getFocused(midi.widChannelRack):
            raise RuntimeError("Sample target changed before opening its menu")
        transport.globalTransport(midi.FPT_ItemMenu, 1, midi.PME_System | midi.PME_FromMIDI)
        return {"status": "menu_requested", "channel": _channel(index)}
    if action == "projectFiles.finishImport":
        index = _index(params.get("index"), channels.channelCount(True), "channel index")
        _verify_destination(index, params.get("before_channels"))
        expected_type = params.get("channel_type")
        if channels.channelCount(True) != params.get("before_count", -1) + 1:
            raise RuntimeError("Channel Rack changed during sample import")
        if channels.getChannelType(index, True) != expected_type:
            raise RuntimeError("Sample destination changed type")
        name = channels.getChannelName(index, True)
        expected_stem = params.get("expected_stem", "")
        initial_name = params.get("initial_name")
        if not isinstance(initial_name, str) or initial_name.casefold() == expected_stem.casefold():
            raise ValueError("The empty channel name cannot verify this source filename")
        if not expected_stem or name.casefold() != expected_stem.casefold():
            return {"status": "waiting", "channel": _channel(index)}
        route = params.get("mixer_track")
        if route is not None:
            _index(route, mixer.trackCount() - 1, "mixer track")
            channels.setTargetFxTrack(index, route, True)
            if channels.getTargetFxTrack(index, True) != route:
                raise RuntimeError("FL Studio did not apply the requested sample routing")
        new_name = params.get("channel_name")
        if new_name is not None:
            if not isinstance(new_name, str) or not new_name.strip() or len(new_name) > 128:
                raise ValueError("channel_name must have 1..128 characters")
            channels.setChannelName(index, new_name, True)
            if channels.getChannelName(index, True) != new_name:
                raise RuntimeError("FL Studio did not apply the requested channel name")
        return {
            "status": "imported",
            "channel": _channel(index),
            "verification": "new_channel_type_and_sample_filename_label",
        }
    raise ValueError("Unknown project file action: " + action)
