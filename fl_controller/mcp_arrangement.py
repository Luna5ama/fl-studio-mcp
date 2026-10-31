"""Pattern, tempo and Playlist metadata using the documented FL MIDI API.

Indexes are FL's one-based pattern/Playlist indexes. Time is integer PPQ ticks.
Ordinary Playlist clips have no public MIDI enumeration/mutation API; native
workflows are implemented by the server and verified with selection boundaries.
"""

import math

import arrangement
import channels
import general
import midi
import mixer
import patterns
import playlist
import transport
import ui


def _integer(value, name, minimum=1, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(name + " is outside the supported integer range")
    return value


def _pattern_index(value, used=True, allow_active_default=False):
    index = _integer(value, "pattern_index", 1, patterns.patternMax())
    if (
        used
        and patterns.isPatternDefault(index)
        and not (allow_active_default and index == patterns.patternNumber())
    ):
        raise ValueError("Pattern does not contain saved data or a custom name")
    return index


def _pattern_info(index):
    # Windows MIDI API 45 render measurements confirm sixteenth-note steps,
    # despite the published API calling this value beats. Public beats are quarters.
    steps = patterns.getPatternLength(index)
    return {
        "index": index,
        "name": patterns.getPatternName(index),
        "length_beats": steps / 4,
        "length_steps": steps,
        "length_raw_unit": "sixteenth_note_steps",
        "length_unit_assumed": general.getVersion() != 45,
        "color": patterns.getPatternColor(index),
        "is_default": bool(patterns.isPatternDefault(index)),
        "selected": bool(patterns.isPatternSelected(index)),
        "active": patterns.patternNumber() == index,
    }


def _length_steps(beats):
    if isinstance(beats, bool) or not isinstance(beats, (int, float)) or not math.isfinite(beats):
        raise ValueError("length_beats must be a finite number of quarter-note beats")
    steps = beats * 4
    if not 1 <= steps <= 65536 or int(steps) != steps:
        raise ValueError("length_beats must map to 1..65536 whole sixteenth-note steps")
    return int(steps)


def _unused_pattern(requested=None, avoid=()):
    if requested is not None:
        index = _pattern_index(requested, used=False)
        if index in avoid or not patterns.isPatternDefault(index):
            raise ValueError("Destination pattern is already used")
        return index
    for index in range(1, patterns.patternMax() + 1):
        if index not in avoid and patterns.isPatternDefault(index):
            return index
    raise ValueError("No unused pattern slots remain")


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise ValueError("name must contain 1 to 255 characters")
    return value.strip()


def _stopped():
    if transport.isPlaying() or transport.isRecording():
        raise RuntimeError("Stop playback/recording before editing the arrangement")
    if hasattr(general, "safeToEdit") and not general.safeToEdit():
        raise RuntimeError("FL Studio is not ready to edit")


def _tempo_mode():
    """Select quarter-note tempo without assuming the UI BPM range or flag ordering."""
    values = [(mode, mixer.getCurrentTempo(mode)) for mode in (False, True)]
    valid = [
        (mode, value)
        for mode, value in values
        if not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    ]
    if len(valid) == 2 and math.isclose(
        max(value for mode, value in valid),
        min(value for mode, value in valid) * 1000,
        rel_tol=1e-6,
    ):
        mode, value = min(valid, key=lambda item: item[1])
        return float(value), mode
    for mode, value in valid:
        if isinstance(value, float):
            return value, mode
    if valid:
        return float(valid[0][1]), valid[0][0]
    raise RuntimeError("Neither tempo mode returned a finite positive quarter-note BPM")


def _timing():
    ppq = general.getRecPPQ()
    ppb = general.getRecPPB()
    return {
        "quarter_bpm": _tempo_mode()[0],
        "tempo_unit": "quarter_note_bpm",
        "ppq": ppq,
        "ticks_per_bar": ppb,
        "quarter_beats_per_bar": ppb / ppq if ppq else None,
        "numerator": None,
        "numerator_readback_supported": False,
        "denominator": None,
        "denominator_readback_supported": False,
        "time_signature_marker_coverage": "global_only",
    }


def _selection():
    return {"start_ticks": arrangement.selectionStart(), "end_ticks": arrangement.selectionEnd()}


def _track_info(index):
    return {
        "index": index,
        "name": playlist.getTrackName(index),
        "color": playlist.getTrackColor(index),
        "muted": bool(playlist.isTrackMuted(index)),
        "selected": bool(playlist.isTrackSelected(index)),
    }


def dispatch(action, params):
    if not action.startswith("arrangement."):
        return None
    command = action.split(".", 1)[1]
    if command == "getPatterns":
        include_defaults = params.get("include_defaults", False)
        records = [
            _pattern_info(index)
            for index in range(1, patterns.patternMax() + 1)
            if include_defaults
            or not patterns.isPatternDefault(index)
            or index == patterns.patternNumber()
        ]
        return {
            "success": True,
            "patterns": records,
            "active_pattern": patterns.patternNumber(),
            "modified_pattern_count": patterns.patternCount(),
            "index_base": 1,
        }
    if command == "getPattern":
        return {
            "success": True,
            "pattern": _pattern_info(_pattern_index(params.get("index"), False)),
        }
    if command == "getTiming":
        return {"success": True, **_timing()}
    if command == "getSelection":
        return {"success": True, **_selection(), "unit": "ticks"}
    if command == "openPlaylistMenu":
        raise RuntimeError(
            "Open the Playlist caption menu through the Windows helper; "
            "the generic FPT_Menu command opens FL's main File menu"
        )
    if command == "getHint":
        return {"success": True, "hint": ui.getHintMsg()}
    if command == "getPlaylist":
        start = _integer(params.get("start_track", 1), "start_track", 1, playlist.trackCount())
        count = _integer(params.get("count", 32), "count", 1, 500)
        end = min(playlist.trackCount() + 1, start + count)
        return {
            "success": True,
            "tracks": [_track_info(index) for index in range(start, end)],
            "track_count": playlist.trackCount(),
            "performance_mode": bool(playlist.getPerformanceModeState()),
            "selection": _selection(),
            "timing": _timing(),
            "clip_coverage": "not_exposed_by_midi_api",
            "index_base": 1,
        }
    _stopped()
    if command == "createPattern":
        name = _name(params.get("name"))
        length = params.get("length_beats")
        steps = _length_steps(length) if length is not None else None
        if steps is not None:
            if not hasattr(patterns, "setPatternLength"):
                raise RuntimeError("Pattern length setting requires MIDI API 39+")
        index = _unused_pattern(params.get("index"), (patterns.patternNumber(),))
        patterns.jumpToPattern(index)
        patterns.setPatternName(index, name)
        if steps is not None:
            patterns.setPatternLength(index, steps)
        record = _pattern_info(index)
        if (
            record["name"] != name
            or not record["active"]
            or (steps is not None and record["length_steps"] != steps)
        ):
            raise RuntimeError("FL Studio did not create/select the requested pattern")
        return {"success": True, "status": "created", "pattern": record}
    if command == "selectPattern":
        index = _pattern_index(params.get("index"), allow_active_default=True)
        patterns.deselectAll()
        patterns.jumpToPattern(index)
        if patterns.patternNumber() != index:
            raise RuntimeError("Pattern selection was not applied")
        return {"success": True, "pattern": _pattern_info(index)}
    if command == "renamePattern":
        index = _pattern_index(params.get("index"), allow_active_default=True)
        name = _name(params.get("name"))
        patterns.setPatternName(index, name)
        if patterns.getPatternName(index) != name:
            raise RuntimeError("Pattern name readback does not match")
        return {"success": True, "pattern": _pattern_info(index)}
    if command == "clonePattern":
        source = _pattern_index(params.get("index"))
        destination = _unused_pattern(params.get("destination"), (source, patterns.patternNumber()))
        name = _name(params.get("name") or (patterns.getPatternName(source) + " copy"))
        if general.getVersion() < 43:
            raise RuntimeError("Explicit-destination pattern cloning requires MIDI API 43+")
        patterns.clonePattern(source, destination)
        patterns.setPatternName(destination, name)
        patterns.jumpToPattern(destination)
        record = _pattern_info(destination)
        if (
            record["is_default"]
            or record["name"] != name
            or record["length_steps"] != patterns.getPatternLength(source)
        ):
            raise RuntimeError("Pattern clone verification failed; inspect both patterns")
        return {
            "success": True,
            "status": "cloned",
            "source_index": source,
            "pattern": record,
            "verification": "destination_identity_and_length",
        }
    if command == "preparePatternDelete":
        index = _pattern_index(params.get("index"))
        expected = params.get("expected_name")
        if not isinstance(expected, str) or patterns.getPatternName(index) != expected:
            raise ValueError("Pattern identity changed; read the pattern list before deleting")
        patterns.deselectAll()
        patterns.jumpToPattern(index)
        if patterns.patternNumber() != index:
            raise RuntimeError("Pattern deletion target could not be selected; nothing was deleted")
        return {
            "success": True,
            "pattern": _pattern_info(index),
            "modified_pattern_count": patterns.patternCount(),
        }
    if command == "verifyPatternDelete":
        expected_count = _integer(params.get("before_count"), "before_count") - 1
        count = patterns.patternCount()
        return {
            "success": count == expected_count,
            "status": "deleted" if count == expected_count else "unknown",
            "modified_pattern_count": count,
            "error": None
            if count == expected_count
            else "Pattern count did not decrease by one; do not retry deletion automatically",
        }
    if command == "setTempo":
        if "display_bpm" not in params or "quarter_bpm" not in params:
            raise ValueError(
                "setTempo requires explicit display_bpm and quarter_bpm; "
                "a quarter-only or legacy bpm write is ambiguous"
            )
        displayed = params["display_bpm"]
        expected = params["quarter_bpm"]
        for value, name in ((displayed, "display_bpm"), (expected, "quarter_bpm")):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(name + " must be a finite number")
        if not 10 <= displayed <= 522:
            raise ValueError("display_bpm must be between 10 and 522")
        if not 0 < expected <= 2088:
            raise ValueError("quarter_bpm must be positive and no greater than 2088")
        if not hasattr(mixer, "setCurrentTempo"):
            raise RuntimeError("Tempo setting requires MIDI API 38+")
        unused, mode = _tempo_mode()
        # Windows API45 is asymmetric: getter reports quarter-note tempo, while
        # setter consumes the displayed tempo (6/8: write160 -> read80).
        mixer.setCurrentTempo(float(displayed), mode)
        actual, unused = _tempo_mode()
        if abs(actual - expected) > 0.01:
            raise RuntimeError("Quarter-note tempo readback does not match the expected value")
        return {"success": True, "display_bpm_written": float(displayed), **_timing()}
    if command == "setTimeSignature":
        numerator = _integer(params.get("numerator"), "numerator", 1, 32)
        denominator = params.get("denominator")
        if type(denominator) is not int or denominator not in (1, 2, 4, 8, 16, 32):
            raise ValueError("denominator must be one of 1, 2, 4, 8, 16, 32")
        if not hasattr(general, "setNumerator") or not hasattr(general, "setDenominator"):
            raise RuntimeError("Time signature setting requires MIDI API 38+")
        general.setNumerator(numerator)
        general.setDenominator(denominator)
        actual = _timing()
        if actual["ticks_per_bar"] * denominator != numerator * actual["ppq"] * 4:
            raise RuntimeError("Time signature bar-tick ratio readback does not match")
        return {
            "success": True,
            "status": "applied",
            **actual,
            "numerator_requested": numerator,
            "denominator_requested": denominator,
            "verification": "bar_tick_ratio_readback; no independent signature getters",
        }
    if command == "setTrack":
        index = _integer(params.get("track"), "track", 1, playlist.trackCount())
        name = _name(params["name"]) if "name" in params else None
        if "muted" in params and type(params["muted"]) is not bool:
            raise ValueError("muted must be boolean")
        if name is not None:
            playlist.setTrackName(index, name)
        if "muted" in params:
            playlist.muteTrack(index, int(params["muted"]))
        record = _track_info(index)
        if (name is not None and record["name"] != name) or (
            "muted" in params and record["muted"] != params["muted"]
        ):
            raise RuntimeError("Playlist track readback does not match; inspect before retrying")
        return {"success": True, "track": record}
    if command == "preparePlaylist":
        if playlist.getPerformanceModeState():
            raise RuntimeError("Disable Playlist Performance mode before editing ordinary clips")
        track = _integer(params.get("track", 1), "track", 1, playlist.trackCount())
        bar = _integer(params.get("bar", 1), "bar", 1, 1000000)
        if "pattern_index" in params and "source_channel" in params:
            raise ValueError("Specify exactly one pattern or Audio Clip source")
        source = None
        if "pattern_index" in params:
            index = _pattern_index(params["pattern_index"])
            source = {"kind": "pattern", **_pattern_info(index)}
        if "source_channel" in params:
            channel = _integer(
                params["source_channel"], "source_channel", 0, channels.channelCount(True) - 1
            )
            if channels.getChannelType(channel, True) != 4:
                raise ValueError("source_channel must be an Audio Clip channel (type 4)")
            source = {
                "kind": "audio",
                "index": channel,
                "name": channels.getChannelName(channel, True),
            }
        if not hasattr(playlist, "selectTool"):
            raise RuntimeError("Playlist native editing requires MIDI API 40+ (selectTool)")
        if "pattern_index" in params:
            patterns.deselectAll()
            patterns.jumpToPattern(index)
            if patterns.patternNumber() != index:
                raise RuntimeError("Requested pattern could not be selected")
        if "source_channel" in params:
            channels.selectOneChannel(channel, True)
        ui.showWindow(midi.widPlaylist)
        ui.setFocused(midi.widPlaylist)
        playlist.selectTool(getattr(midi, "EETool_Pencil", 0))
        if params.get("scroll", True):
            ui.scrollWindow(midi.widPlaylist, track, 0)
            ui.scrollWindow(midi.widPlaylist, bar, 1)
        tracks = [_track_info(index) for index in range(1, playlist.trackCount() + 1)]
        names = [
            patterns.getPatternName(index)
            for index in range(1, patterns.patternMax() + 1)
            if not patterns.isPatternDefault(index)
        ]
        names.extend(
            channels.getChannelName(index, True) for index in range(channels.channelCount(True))
        )
        if source is not None:
            source["name_occurrences"] = names.count(source["name"])
        return {
            "success": True,
            "track": track,
            "bar": bar,
            "tracks": tracks,
            "source_names": sorted(set(names)),
            "timing": _timing(),
            "source": source,
        }
    raise ValueError("Unknown arrangement action: " + command)
