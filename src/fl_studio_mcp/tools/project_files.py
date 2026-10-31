"""Sample import, explicit FLP saving, and verified Windows WAV rendering."""

from __future__ import annotations

import math
import time
from typing import Literal

from fl_studio_mcp.utils.connection import get_connection
from fl_studio_mcp.utils.project_files_windows import (
    create_sample_channel,
    input_audio_path,
    output_path,
    prepare_playlist,
    render_wav_file,
    save_project_file,
)


def _timeout(value, maximum):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError("timeout must be a finite number")
    if not 5 <= value <= maximum:
        raise ValueError(f"timeout must be between 5 and {maximum} seconds")
    return float(value)


def _wait_transport_mode(conn, expected, timeout=3):
    deadline = time.monotonic() + timeout
    while True:
        actual = conn.send_command("transport.getStatus")
        if actual.get("success") and actual.get("loop_mode") == expected:
            return actual
        if time.monotonic() >= deadline:
            raise RuntimeError("FL Studio did not confirm Song/Pattern mode " + expected)
        time.sleep(0.05)


def register_project_file_tools(mcp):
    @mcp.tool()
    def fl_import_sample(
        path: str,
        channel_type: Literal["sampler", "audio_clip"] = "sampler",
        channel_name: str | None = None,
        mixer_track: int | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """Import an existing audio file into a NEW Sampler or Audio Clip channel.

        Audio Clip channels can be placed in Playlist by the arrangement tools.
        Requires Windows/English FL UI in foreground. Uses a new destination so
        existing instruments are not replaced. A late failure can leave the new
        channel behind; inspect the project before retrying.
        """
        started = False
        try:
            source = input_audio_path(path)
            if source.stem.casefold() in ("sampler", "audio clip"):
                raise ValueError(
                    "Rename this source file: its stem matches an empty sample channel "
                    "and cannot prove that FL loaded the file"
                )
            timeout = _timeout(timeout, 300)
            if channel_type not in ("sampler", "audio_clip"):
                raise ValueError("channel_type must be sampler or audio_clip")
            if channel_name is not None and (
                not channel_name.strip()
                or len(channel_name) > 128
                or any(c in channel_name for c in "\r\n\x00")
            ):
                raise ValueError("channel_name must contain 1..128 ordinary characters")
            if mixer_track is not None and (type(mixer_track) is not int or mixer_track < 0):
                raise ValueError("mixer_track must be a nonnegative integer")
            conn = get_connection()
            with conn.transaction():
                if mixer_track is not None:
                    tracks = conn.send_command("mixer.getTrackCount")
                    if not tracks.get("success"):
                        return tracks
                    if mixer_track >= tracks["count"] - 1:
                        raise ValueError("The Current utility track is not a sample destination")
                started = True
                result = create_sample_channel(
                    conn, source, channel_type, channel_name, mixer_track, timeout
                )
                result.setdefault("success", result.get("status") == "imported")
                return result
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            return {
                "success": False,
                "status": "unknown" if started else "rejected",
                "error": str(exc),
                "inspect_project_before_retry": started,
            }

    @mcp.tool()
    def fl_save_project(path: str, overwrite: bool = False, timeout: float = 30.0) -> dict:
        """Save As to an explicit absolute .flp filename and verify the written FLP.

        Existing files are refused unless overwrite=True. This changes FL's current
        project filename. A path is mandatory because the MIDI API cannot reliably
        identify the current on-disk FLP path. Requires English Windows FL UI.
        """
        started = False
        try:
            destination = output_path(path, ".flp", overwrite)
            timeout = _timeout(timeout, 300)
            conn = get_connection()
            with conn.transaction():
                prepare_playlist(conn)
                started = True
                result = save_project_file(destination, overwrite, timeout)
                status = conn.send_command("projectFiles.status")
                if not status.get("success") or status.get("changed") != 0:
                    return {
                        "success": False,
                        "status": "unknown",
                        "file": result,
                        "error": "FLP exists but FL Studio did not confirm a clean saved project",
                        "controller_status": status,
                    }
                return {
                    "success": True,
                    **result,
                    "project_changed": False,
                    "verification": "complete_flp_file_and_clean_project",
                }
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            return {
                "success": False,
                "status": "unknown" if started else "rejected",
                "error": str(exc),
                "inspect_project_before_retry": started,
            }

    @mcp.tool()
    def fl_export_wav(
        path: str,
        render_scope: Literal["full_song", "current_selection", "pattern"] = "full_song",
        overwrite: bool = False,
        timeout: float = 600.0,
    ) -> dict:
        """Render WAV, validate complete audio frames, and publish to an explicit path.

        full_song clears the Playlist time selection and reports its before/after
        boundaries; current_selection requires a positive existing selection;
        pattern renders the selected Pattern. Song/Pattern mode is restored even
        after errors, with restoration status reported. Playback remains stopped.
        Actual sample rate, bit depth, channels and frames come from the WAV header.
        Additional enabled formats/stems stay in a unique staging folder.
        Never auto-retry an uncertain timeout while FL may still be rendering.
        """
        try:
            destination = output_path(path, ".wav", overwrite)
            timeout = _timeout(timeout, 7200)
            if render_scope not in ("full_song", "current_selection", "pattern"):
                raise ValueError("Invalid render_scope")
            conn = get_connection()
            with conn.transaction():
                before = conn.send_command("transport.getStatus")
                mode_before = before.get("loop_mode")
                if not before.get("success") or mode_before not in ("song", "pattern"):
                    raise RuntimeError("Cannot read the original Song/Pattern mode")
                selection = conn.send_command("arrangement.getSelection")
                if not selection.get("success"):
                    raise RuntimeError("Cannot read the current Playlist time selection")
                selection_before = {key: selection[key] for key in ("start_ticks", "end_ticks")}
                if render_scope == "current_selection" and selection_before["end_ticks"] <= max(
                    0, selection_before["start_ticks"]
                ):
                    raise ValueError("No positive Playlist time selection is active")
                started = False
                mode_attempted = False
                result = None
                try:
                    prepare_playlist(conn, stop_playback=True)
                    target_mode = "pattern" if render_scope == "pattern" else "song"
                    mode_attempted = True
                    mode = conn.send_command("transport.setLoopMode", {"mode": target_mode})
                    check = (
                        _wait_transport_mode(conn, target_mode)
                        if mode.get("success")
                        else {"success": False}
                    )
                    if (
                        not mode.get("success")
                        or not check.get("success")
                        or check.get("loop_mode") != target_mode
                    ):
                        raise RuntimeError("FL Studio did not confirm the requested render mode")
                    started = True
                    rendered = render_wav_file(conn, destination, overwrite, timeout, render_scope)
                    result = {
                        "success": True,
                        **rendered,
                        "verification": "complete_wav_and_render_ui_terminal",
                    }
                except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                    result = {
                        "success": False,
                        "status": "unknown" if started else "rejected",
                        "error": str(exc),
                        "inspect_render_before_retry": started,
                    }
                finally:
                    restoration = {
                        "attempted": mode_attempted,
                        "mode": mode_before,
                        "success": not mode_attempted,
                    }
                    if mode_attempted:
                        try:
                            restored = conn.send_command(
                                "transport.setLoopMode", {"mode": mode_before}
                            )
                            actual = (
                                _wait_transport_mode(conn, mode_before)
                                if restored.get("success")
                                else conn.send_command("transport.getStatus")
                            )
                            restoration.update(
                                success=bool(
                                    restored.get("success")
                                    and actual.get("success")
                                    and actual.get("loop_mode") == mode_before
                                ),
                                actual_mode=actual.get("loop_mode"),
                            )
                            if not restoration["success"]:
                                restoration["error"] = (
                                    "Original Song/Pattern mode was not confirmed"
                                )
                        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                            restoration.update(success=False, error=str(exc))
                    try:
                        after = conn.send_command("arrangement.getSelection")
                        selection_after = (
                            {key: after[key] for key in ("start_ticks", "end_ticks")}
                            if after.get("success")
                            else None
                        )
                    except (OSError, ValueError, RuntimeError, TimeoutError):
                        selection_after = None
                    if result is not None:
                        result.update(
                            transport_mode_restoration=restoration,
                            selection_before=selection_before,
                            selection_after=selection_after,
                            changed_selection=(
                                selection_before != selection_after
                                if selection_after is not None
                                else None
                            ),
                        )
                        if result["success"] and not restoration["success"]:
                            result.update(
                                success=False,
                                status="rendered_with_restore_error",
                                verified_audio=True,
                                error=restoration["error"],
                            )
                return result
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            return {
                "success": False,
                "status": "rejected",
                "error": str(exc),
                "inspect_render_before_retry": False,
            }
