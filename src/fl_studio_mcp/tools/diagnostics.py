"""Environment diagnostics and honest in-process loading operation status."""

from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import sys

from fl_studio_mcp.utils.connection import get_connection
from fl_studio_mcp.utils.fl_paths import get_user_data_dir
from fl_studio_mcp.utils.runtime_operations import load_operations


def register_diagnostic_tools(mcp):
    @mcp.tool()
    def fl_get_load_operations(limit: int = 20) -> dict:
        """List this MCP server session's plugin loads, including active IDs and progress."""
        try:
            return {
                "success": True,
                "scope": "server_session",
                "operations": load_operations.list(limit),
            }
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

    @mcp.tool()
    def fl_cancel_plugin_load(operation_id: str | None = None) -> dict:
        """Request cancellation of an active load; poll its final status afterward.

        Omit the id only when exactly one load is active. Cancellation never undoes
        a plugin activation already issued and does not mean the project was reverted.
        """
        try:
            return load_operations.cancel(operation_id)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

    @mcp.tool()
    def fl_get_project_summary() -> dict:
        """Read current project metadata, displayed tempo and object counts.

        Requires Windows, an English FL UI, English OCR and a visible, unobstructed tempo toolbar.
        tempo/bpm and beats_per_bar use displayed beats; quarter_bpm and
        quarter_beats_per_bar remain in quarter-note units. Unreadable tempo
        returns an error rather than a partially converted project summary.
        """
        try:
            from fl_studio_mcp.utils.tempo_windows import read_display_tempo

            connection = get_connection()
            with connection.transaction():
                result = connection.send_command("system.getProjectSummary")
                if not result.get("success"):
                    return result
                displayed = read_display_tempo(connection)
                mapped = dict(result)
                mapped.update(
                    {
                        key: displayed[key]
                        for key in ("bpm", "quarter_bpm", "tempo_unit", "ui_to_quarter_ratio")
                    }
                )
                mapped["tempo"] = displayed["bpm"]
                mapped["beats_per_bar"] = (
                    result["quarter_beats_per_bar"] * displayed["ui_to_quarter_ratio"]
                )
                mapped["beats_per_bar_unit"] = "displayed_beats"
                mapped["tempo_verification"] = displayed.get("verification")
                return mapped
        except (RuntimeError, OSError, ImportError) as exc:
            return {"success": False, "error": str(exc)}

    @mcp.tool()
    def fl_check_environment(probe_fl: bool = True) -> dict:
        """Check Windows dependencies, controller install identity, MIDI and FL capabilities.

        Opening a MIDI port alone is not a handshake. controller_response.success
        must be true before reporting FL communication as verified.
        """
        directory = get_user_data_dir() / "Settings" / "Hardware" / "FLStudioMCP"
        installed = directory / "device_FLStudioMCP.py"
        result = {
            "success": True,
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "user_data_dir": str(get_user_data_dir()),
            "controller_installed": installed.is_file(),
            "loading_platform_supported": sys.platform == "win32",
            "dependencies": {},
            "notes": [],
        }
        for name in (
            "fastmcp",
            "mido",
            "python-rtmidi",
            "pynput",
            "pillow",
            "winrt-Windows.Media.Ocr",
        ):
            try:
                result["dependencies"][name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                result["dependencies"][name] = None
        if installed.is_file():
            result["controller_sha256"] = hashlib.sha256(installed.read_bytes()).hexdigest()
        if sys.platform == "win32":
            try:
                from winrt.windows.globalization import Language
                from winrt.windows.media.ocr import OcrEngine

                result["english_ocr_available"] = bool(
                    OcrEngine.is_language_supported(Language("en-US"))
                )
            except Exception as exc:
                result["english_ocr_available"] = False
                result["notes"].append(str(exc))
        if probe_fl:
            try:
                conn = get_connection()
                result["midi"] = conn.get_status()
                result["controller_response"] = conn.send_command("system.getCapabilities")
                result["communication_verified"] = bool(
                    result["controller_response"].get("success")
                    and not result["controller_response"].get("error")
                    and result["controller_response"].get("protocol_version") == 2
                    and isinstance(result["controller_response"].get("midi_api_version"), int)
                )
                result["midi"] = conn.get_status()
            except (RuntimeError, OSError) as exc:
                result["communication_verified"] = False
                result["notes"].append(str(exc))
        else:
            result["communication_verified"] = None
        return result
