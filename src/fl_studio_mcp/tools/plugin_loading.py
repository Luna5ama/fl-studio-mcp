"""Scanned plugin loading with correlated status, native activation and cancellation."""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING, Literal

from fl_studio_mcp.utils.connection import get_connection
from fl_studio_mcp.utils.plugin_catalog import list_plugins, resolve_plugin
from fl_studio_mcp.utils.runtime_operations import load_operations
from fl_studio_mcp.utils.windows_plugin_menu import drag_effect_to_slot, select_plugin_menu_action
from fl_studio_mcp.utils.windows_ui import FLWindows

if TYPE_CHECKING:
    from fastmcp import FastMCP


def _execute_load(conn, entry, kind, mixer_track, timeout):
    operation_id = None
    try:
        result = conn.send_command(
            "plugins.beginLoad",
            {
                "plugin_id": entry["id"],
                "kind": kind,
                "mixer_track": mixer_track,
                "timeout": timeout,
            },
        )
        if not result.get("success") or result.get("error"):
            return result
        operation_id = result["operation_id"]
        load_operations.begin(operation_id, plugin_id=entry["id"], kind=kind, runner_active=True)
        deadline = time.monotonic() + timeout
        activated = False
        cancel_sent = False
        while time.monotonic() < deadline:
            if load_operations.cancelled(operation_id) and not cancel_sent:
                conn.send_command(
                    "plugins.cancelLoad",
                    {
                        "operation_id": operation_id,
                        "error": "Cancellation requested by caller",
                    },
                )
                cancel_sent = True
            time.sleep(0.1)
            result = conn.send_command("plugins.getLoadStatus", {"operation_id": operation_id})
            if not result.get("success") and "Timeout waiting" in result.get("error", ""):
                continue  # Retry only observation, never activation.
            result.setdefault("operation_id", operation_id)
            if not result.get("status"):
                result["status"] = "unknown"
            load_operations.update(result)
            if not result.get("success") or result["status"] != "loading":
                return result
            if result.get("stage") in ("awaiting_menu", "awaiting_drag") and not activated:

                def confirm():
                    if load_operations.cancelled(operation_id):
                        raise RuntimeError("Cancelled before plugin activation")
                    confirmation = conn.send_command(
                        "plugins.confirmLoadMenu", {"operation_id": operation_id}
                    )
                    if not confirmation.get("success") or confirmation.get("error"):
                        raise RuntimeError(confirmation.get("error", "Destination changed"))

                activated = True
                try:
                    if result["stage"] == "awaiting_drag":
                        drag_effect_to_slot(entry["name"], result["destination_slot"], confirm)
                    else:
                        select_plugin_menu_action(result["menu_action"], confirm)
                except Exception as exc:
                    cancellation = conn.send_command(
                        "plugins.cancelLoad",
                        {
                            "operation_id": operation_id,
                            "error": str(exc),
                        },
                    )
                    status = cancellation.get("status", "unknown")
                    if status == "loading":
                        status = "unknown"  # A late activation cannot be ruled out.
                    result = {
                        "success": False,
                        "status": status,
                        "operation_id": operation_id,
                        "error": str(exc),
                        "activation_may_have_occurred": status == "unknown",
                    }
                    load_operations.update(result)
                    return result
        result = {
            "success": False,
            "operation_id": operation_id,
            "status": "unknown",
            "error": "Loading timed out. Inspect FL Studio before retrying; do not auto-retry.",
        }
        load_operations.update(result)
        return result
    except Exception as exc:
        result = {"success": False, "error": str(exc)}
        if operation_id is not None:
            result.update(operation_id=operation_id, status="unknown")
            load_operations.update(result)
        return result
    finally:
        if operation_id is not None:
            state = load_operations.get(operation_id)
            if state is not None:
                if state["status"] == "loading":
                    state["status"] = "unknown"
                state["runner_active"] = False
                load_operations.update(state)


def register_plugin_loading_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def fl_get_plugin_load_status(operation_id: str) -> dict:
        """Inspect a load; only loaded confirms completion. Never retry an uncertain add."""
        cached = load_operations.get(operation_id)
        if cached is not None and cached.get("runner_active") and cached["status"] == "loading":
            return cached
        try:
            result = get_connection().send_command(
                "plugins.getLoadStatus",
                {
                    "operation_id": operation_id,
                },
            )
            if result.get("status"):
                load_operations.update(result)
            if (not result.get("success") or result.get("error")) and cached is not None:
                return dict(cached, controller_status_error=result.get("error"))
            return result
        except RuntimeError as exc:
            return dict(cached or {}, success=False, error=str(exc))

    @mcp.tool()
    def fl_list_available_plugins(
        query: str = "",
        kind: Literal["all", "generator", "effect"] = "all",
    ) -> dict:
        """List exact catalog IDs of plugins already scanned in FL's Plugin Manager."""
        try:
            entries = list_plugins(query, kind)
            return {"success": True, "plugins": entries, "count": len(entries)}
        except (ValueError, OSError) as exc:
            return {"success": False, "error": str(exc)}

    @mcp.tool()
    def fl_add_plugin(
        plugin: str,
        kind: Literal["generator", "effect"],
        mixer_track: int | None = None,
        timeout: float = 60.0,
    ) -> dict:
        """Add and verify a scanned VST/VST3 instrument or mixer effect.

        Instruments get a new channel; effects use an empty slot on explicit mixer_track.
        Optional mixer_track routes a new instrument. Indices are zero-based. Requires
        Windows, English FL UI, local English OCR, updated controller and MIDI API 34+.
        Expand Browser and expose Mixer slots; leave FL untouched during loading.
        Inspect uncertain outcomes before retrying. Installation/scanning is separate.
        """
        try:
            if kind not in ("generator", "effect"):
                raise ValueError("kind must be generator or effect")
            if not 5 <= timeout <= 180:
                raise ValueError("timeout must be between 5 and 180 seconds")
            if kind == "effect" and mixer_track is None:
                raise ValueError("mixer_track is required for effects")
            if mixer_track is not None and (type(mixer_track) is not int or mixer_track < 0):
                raise ValueError("mixer_track must be a nonnegative integer")
            entry = resolve_plugin(plugin, kind)
            if sys.platform != "win32":
                raise ValueError("Automatic plugin loading currently requires Windows")
            conn = get_connection()
            with conn.transaction():
                # Resolve Windows foreground ownership before FL starts its
                # asynchronous Browser navigation or opens any plugin menu.
                with FLWindows():
                    return _execute_load(conn, entry, kind, mixer_track, timeout)
        except (ValueError, OSError, RuntimeError) as exc:
            return {"success": False, "error": str(exc)}
