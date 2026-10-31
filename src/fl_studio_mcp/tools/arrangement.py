"""Pattern, timing and explicit Playlist clip workflows."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from fl_studio_mcp.utils.connection import get_connection

if TYPE_CHECKING:
    from fastmcp import FastMCP


def _display_tempo(connection):
    from fl_studio_mcp.utils.tempo_windows import read_display_tempo

    return read_display_tempo(connection)


def _public_timing(connection, result):
    target = result if ("quarter_bpm" in result or "bpm" in result) else result.get("timing")
    if not isinstance(target, dict):
        return result
    displayed = _display_tempo(connection)
    quarter = target.get("quarter_bpm", target.get("bpm"))
    if quarter is not None and not math.isclose(quarter, displayed["quarter_bpm"], abs_tol=1e-8):
        raise RuntimeError("Tempo changed while reading timing metadata")
    mapped = dict(target)
    mapped.update(
        {key: displayed[key] for key in ("bpm", "quarter_bpm", "tempo_unit", "ui_to_quarter_ratio")}
    )
    mapped["tempo_verification"] = displayed.get("verification")
    if target is result:
        return mapped
    return dict(result, timing=mapped)


def _set_displayed_tempo(bpm):
    started = False
    try:
        if (
            isinstance(bpm, bool)
            or not isinstance(bpm, (int, float))
            or not math.isfinite(bpm)
            or not 10 <= bpm <= 522
        ):
            raise ValueError("Displayed BPM must be a finite number between 10 and 522")
        connection = get_connection()
        with connection.transaction():
            before = _display_tempo(connection)
            quarter = float(bpm) / before["ui_to_quarter_ratio"]
            started = True
            result = connection.send_command(
                "arrangement.setTempo",
                {"display_bpm": float(bpm), "quarter_bpm": quarter},
                timeout=10.0,
            )
            if not result.get("success"):
                return dict(result, status="unknown", inspect_project_before_retry=True)
            result = _public_timing(connection, result)
            if not math.isclose(result["bpm"], bpm, rel_tol=0, abs_tol=0.0011):
                return dict(
                    result,
                    success=False,
                    status="unknown",
                    error="FL's displayed tempo did not match the requested BPM",
                    inspect_project_before_retry=True,
                )
            return result
    except (ValueError, RuntimeError, OSError) as exc:
        return {
            "success": False,
            "status": "unknown" if started else "failed",
            "error": str(exc),
            "inspect_project_before_retry": started,
        }


def _api(action, params=None):
    mutation = not action.startswith("get")
    started = False
    try:
        connection = get_connection()
        with connection.transaction():
            if action == "setTimeSignature":
                # The public result uses displayed tempo; reject an unreadable
                # toolbar before changing the host's meter.
                _display_tempo(connection)
            started = True
            result = connection.send_command("arrangement." + action, params or {}, timeout=10.0)
            if result.get("success"):
                result = _public_timing(connection, result)
        if mutation and not result.get("success"):
            return {**result, "status": "unknown", "inspect_project_before_retry": True}
        return result
    except ValueError as exc:
        return {"success": False, "status": "failed", "error": str(exc)}
    except (RuntimeError, OSError) as exc:
        return {
            "success": False,
            "status": "unknown" if mutation and started else "failed",
            "error": str(exc),
            "inspect_project_before_retry": mutation and started,
        }


def _native(function, *args, **kwargs):
    try:
        connection = get_connection()
        with connection.transaction():
            return function(connection, *args, **kwargs)
    except ValueError as exc:
        return {"success": False, "status": "failed", "error": str(exc)}
    except Exception as exc:
        return {
            "success": False,
            "status": "unknown",
            "error": str(exc),
            "inspect_project_before_retry": True,
        }


def _positive(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(name + " must be a positive integer (one-based)")


def register_arrangement_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def fl_get_patterns(include_defaults: bool = False) -> dict:
        """Read patterns directly from FL. Pattern indices are one-based."""
        return _api("getPatterns", {"include_defaults": include_defaults})

    @mcp.tool()
    def fl_create_pattern(
        name: str, length_beats: float | None = None, pattern_index: int | None = None
    ) -> dict:
        """Create/name an unused pattern slot and select it; never overwrite a used slot.

        An empty named pattern is not a melody: populate notes/steps separately.
        Lengths are quarter-note beats, in multiples of 0.25 (sixteenth notes).
        Explicit length setting requires MIDI API 39+; Windows API45 steps are converted.
        """
        return _api(
            "createPattern", {"name": name, "length_beats": length_beats, "index": pattern_index}
        )

    @mcp.tool()
    def fl_select_pattern(pattern_index: int) -> dict:
        """Select the exact existing pattern for subsequent note/step edits."""
        return _api("selectPattern", {"index": pattern_index})

    @mcp.tool()
    def fl_rename_pattern(pattern_index: int, name: str) -> dict:
        """Rename a pattern and verify its resulting name."""
        return _api("renamePattern", {"index": pattern_index, "name": name})

    @mcp.tool()
    def fl_clone_pattern(
        pattern_index: int, name: str | None = None, destination_index: int | None = None
    ) -> dict:
        """Clone pattern data into an unused destination; explicit cloning needs API 43+."""
        return _api(
            "clonePattern", {"index": pattern_index, "name": name, "destination": destination_index}
        )

    @mcp.tool()
    def fl_delete_pattern(pattern_index: int, expected_name: str, confirm: bool = False) -> dict:
        """Delete a pattern through FL's PATTERNS menu with an explicit identity guard.

        confirm=True authorizes deleting its data and affected Playlist instances.
        This deletes the source pattern, unlike fl_delete_playlist_clip which removes
        one occurrence. Windows English UI is required. A failed/unknown result must
        be inspected before retrying; do not automatically repeat deletion.
        """
        from fl_studio_mcp.utils.arrangement_windows import delete_pattern

        return _native(delete_pattern, pattern_index, expected_name, confirm)

    @mcp.tool()
    def fl_get_timing() -> dict:
        """Read FL display BPM plus quarter-note BPM, PPQ and bar length.

        Displayed tempo uses verified Windows toolbar OCR and requires visible FL UI.
        There are no independent MIDI time-signature numerator/denominator getters.
        """
        return _api("getTiming")

    @mcp.tool()
    def fl_set_tempo(bpm: float) -> dict:
        """Set BPM from 10 to 522 to match FL's displayed tempo (Windows, API 38+).

        The toolbar value and quarter-note API value determine the meter conversion;
        the resulting displayed value is read back. Keep FL visible and untouched.
        """
        return _set_displayed_tempo(bpm)

    @mcp.tool()
    def fl_set_time_signature(numerator: int, denominator: int = 4) -> dict:
        """Set global time signature (API 38+), with displayed-BPM verification.

        Requires Windows, English FL UI/OCR, and a visible unobstructed toolbar.
        The toolbar is checked before editing. Readback verifies bar-tick ratio
        (e.g. 6/8 = 3 quarter-note beats), without independently proving numerator
        or denominator. FL may adjust quarter-note tempo
        when the denominator changes. Existing time-signature markers are not edited.
        """
        return _api("setTimeSignature", {"numerator": numerator, "denominator": denominator})

    @mcp.tool()
    def fl_get_playlist(start_track: int = 1, count: int = 32) -> dict:
        """Read Playlist tracks and timeline selection. Ordinary clips are not exposed by MIDI.

        Use fl_get_visible_playlist_clips for bounded UI inspection, never as a complete
        enumeration of hidden/offscreen/stacked clips. Track indices are one-based.
        """
        return _api("getPlaylist", {"start_track": start_track, "count": count})

    @mcp.tool()
    def fl_set_playlist_track(
        track: int, name: str | None = None, muted: bool | None = None
    ) -> dict:
        """Rename or mute a Playlist track; unrelated Mixer tracks are not changed."""
        params = {"track": track}
        if name is not None:
            params["name"] = name
        if muted is not None:
            params["muted"] = muted
        return _api("setTrack", params)

    @mcp.tool()
    def fl_get_visible_playlist_clips() -> dict:
        """Inspect visible, fully readable clip labels and approximate bar positions.

        Requires an unobstructed English Playlist with at least three readable ruler
        labels and consecutive, unique track labels. Hidden/truncated/stacked clips may be omitted.
        Use fl_get_playlist_clip to select one target and read exact time boundaries.
        """
        from fl_studio_mcp.utils.arrangement_windows import inspect_visible_clips

        return _native(inspect_visible_clips)

    @mcp.tool()
    def fl_get_playlist_clip(name: str, track: int, start_bar: int) -> dict:
        """Select one visible named clip and read its exact start/end PPQ ticks.

        Track/bar are one-based. Use unique readable track names; disable grouping
        before editing individual clips. Selection and timeline selection are changed.
        """
        from fl_studio_mcp.utils.arrangement_windows import inspect_clip

        try:
            _positive(track, "track")
            _positive(start_bar, "start_bar")
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        return _native(inspect_clip, name, track, start_bar)

    @mcp.tool()
    def fl_place_pattern_clip(
        pattern_index: int, track: int, start_bar: int = 1, allow_overlap: bool = False
    ) -> dict:
        """Place a pattern at a whole-bar boundary and verify exact selection ticks.

        Windows English UI must show readable ruler/track labels. Changes local snap
        to Bar. Default overlap preflight covers only readable visible labels; it
        cannot prove absence of hidden/stacked clips. Unknown results require inspection.
        """
        from fl_studio_mcp.utils.arrangement_windows import place_clip

        try:
            _positive(pattern_index, "pattern_index")
            _positive(track, "track")
            _positive(start_bar, "start_bar")
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        return _native(
            place_clip, track, start_bar, pattern_index=pattern_index, allow_overlap=allow_overlap
        )

    @mcp.tool()
    def fl_place_audio_clip(
        source_channel: int, track: int, start_bar: int = 1, allow_overlap: bool = False
    ) -> dict:
        """Place an existing Audio Clip channel (type 4), preserving its source file.

        Import with fl_import_sample(channel_type='audio_clip') first. Keep its exact
        name visible in the Audio Clips Picker tab. Whole-bar positions and one-based
        Playlist tracks are used; resulting PPQ tick bounds are verified.
        """
        from fl_studio_mcp.utils.arrangement_windows import place_clip

        try:
            _positive(track, "track")
            _positive(start_bar, "start_bar")
            if type(source_channel) is not int or source_channel < 0:
                raise ValueError("source_channel must be a zero-based channel index")
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        return _native(
            place_clip, track, start_bar, source_channel=source_channel, allow_overlap=allow_overlap
        )

    @mcp.tool()
    def fl_move_playlist_clip(
        name: str, source_track: int, source_bar: int, destination_track: int, destination_bar: int
    ) -> dict:
        """Drag one visible clip to another visible whole-bar/track position and verify bounds.

        Disable clip grouping. Both source and destination must be visible in the same
        calibrated viewport. Offscreen endpoints are refused before input. Moves preserve
        duration and set local snap to Bar; the source Pattern is preserved.
        """
        from fl_studio_mcp.utils.arrangement_windows import move_clip

        try:
            for key, value in (
                ("source_track", source_track),
                ("source_bar", source_bar),
                ("destination_track", destination_track),
                ("destination_bar", destination_bar),
            ):
                _positive(value, key)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        return _native(
            move_clip, name, source_track, source_bar, destination_track, destination_bar
        )

    @mcp.tool()
    def fl_delete_playlist_clip(
        name: str, track: int, start_bar: int, confirm: bool = False
    ) -> dict:
        """Remove one visible clip occurrence, preserving the source Pattern/audio file.

        Requires confirm=True, exact readable name/track/start, grouping disabled and
        an unobstructed Playlist. This never sends Delete with an empty selection.
        Verification requires repeated label absence and stable changed target pixels.
        It covers the visible target, not all hidden project clips.
        """
        from fl_studio_mcp.utils.arrangement_windows import delete_clip

        try:
            _positive(track, "track")
            _positive(start_bar, "start_bar")
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        return _native(delete_clip, name, track, start_bar, confirm)

    @mcp.tool()
    def fl_clear_playlist_time_selection() -> dict:
        """Clear timeline range selection by identified ruler double-click and MIDI readback."""
        from fl_studio_mcp.utils.arrangement_windows import clear_time_selection

        return _native(clear_time_selection)
