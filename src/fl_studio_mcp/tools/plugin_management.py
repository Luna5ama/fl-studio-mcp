"""Project plugin inventory, lifecycle operations and full native FST presets."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Literal

from fl_studio_mcp.utils.connection import get_connection
from fl_studio_mcp.utils.fl_paths import get_user_data_dir
from fl_studio_mcp.utils.plugin_catalog import resolve_plugin
from fl_studio_mcp.utils.plugin_management_windows import (
    FULL_STATE_PARAMETER_LIMIT,
    call,
    delete_plugin,
    identity,
    load_preset,
    move_effect_within_track,
    read_effect_enabled,
    save_preset,
    set_effect_enabled,
)
from fl_studio_mcp.utils.runtime_operations import preset_operations

Kind = Literal["generator", "effect"]


def _target(kind, index, slot_index=-1, expected_plugin=None):
    if kind not in ("generator", "effect") or type(index) is not int or index < 0:
        raise ValueError("Provide a valid plugin kind and nonnegative integer index")
    if kind == "effect" and (type(slot_index) is not int or not 0 <= slot_index < 10):
        raise ValueError("Effects require a slot_index between 0 and 9")
    if kind == "generator" and slot_index != -1:
        raise ValueError("Instrument targets must use slot_index=-1")
    if expected_plugin is not None and (
        not isinstance(expected_plugin, str) or not expected_plugin
    ):
        raise ValueError("expected_plugin must be a nonempty exact original plugin name")
    return {
        "kind": kind,
        "index": index,
        "slot_index": slot_index,
        "expected_plugin": expected_plugin,
    }


def _error(exc):
    return {"success": False, "error": str(exc), "inspect_before_retrying": True}


def register_plugin_management_tools(mcp):
    @mcp.tool()
    def fl_list_plugin_preset_operations(limit: int = 20) -> dict:
        """List this server's FST load progress and IDs, without waiting for MIDI."""
        try:
            return {"success": True, "operations": preset_operations.list(limit)}
        except ValueError as exc:
            return _error(exc)

    @mcp.tool()
    def fl_get_plugin_preset_status(operation_id: str) -> dict:
        """Observe an FST load; active runners publish local status without MIDI waits."""
        cached = None
        try:
            if not isinstance(operation_id, str) or not operation_id:
                raise ValueError("operation_id must be a nonempty string")
            cached = preset_operations.get(operation_id)
            if cached is not None and (
                cached.get("runner_active") or cached["status"] in ("loaded", "cancelled", "failed")
            ):
                return dict(
                    cached, cancellation_requested=preset_operations.cancelled(operation_id)
                )
            result = get_connection().send_command(
                "pluginManagement.status", {"operation_id": operation_id}
            )
            if result.get("status"):
                preset_operations.update(result)
                return result
            if cached is not None:
                return dict(cached, controller_status_error=result.get("error"))
            return result
        except (ValueError, RuntimeError, OSError) as exc:
            return dict(cached or {}, **_error(exc))

    @mcp.tool()
    def fl_cancel_plugin_preset_load(operation_id: str) -> dict:
        """Request cancellation without waiting for the runner's MIDI transaction.

        Before activation the runner sends cancellation to FL and verifies its
        status. Issued activation cannot be undone and remains under observation.
        cancellation_requested is an acknowledgement, not a terminal result.
        """
        try:
            if not isinstance(operation_id, str) or not operation_id:
                raise ValueError("operation_id must be a nonempty string")
            cached = preset_operations.get(operation_id)
            if cached is not None and (
                cached.get("runner_active") or cached["status"] in ("loaded", "cancelled", "failed")
            ):
                return preset_operations.cancel(operation_id)
            result = get_connection().send_command(
                "pluginManagement.cancel", {"operation_id": operation_id}
            )
            if result.get("status"):
                preset_operations.update(result)
            return result
        except (ValueError, RuntimeError, OSError) as exc:
            return _error(exc)

    @mcp.tool()
    def fl_get_project_plugins(include_empty: bool = False) -> dict:
        """Inventory global instrument channels and all Mixer effect instances.

        Indices are zero-based. Per-effect enabled is null when not read from the
        visible Mixer LED; track_effects_enabled describes the entire track only.
        """
        try:
            return get_connection().send_command(
                "pluginManagement.inventory", {"include_empty": include_empty}
            )
        except (RuntimeError, OSError) as exc:
            return _error(exc)

    @mcp.tool()
    def fl_get_plugin_instance(
        kind: Kind, index: int, slot_index: int = -1, parameter_limit: int = 4096
    ) -> dict:
        """Read an explicit instance and its published parameter fingerprint.

        Fingerprints cover only the published parameters, not private VST chunks,
        automation links, or unexposed internal plugin state.
        """
        try:
            target = _target(kind, index, slot_index)
            return get_connection().send_command(
                "pluginManagement.snapshot", dict(target, parameter_limit=parameter_limit)
            )
        except (ValueError, RuntimeError, OSError) as exc:
            return _error(exc)

    @mcp.tool()
    def fl_set_effect_mix(
        mixer_track: int, slot_index: int, value: float, expected_plugin: str | None = None
    ) -> dict:
        """Set and verify a slot's host dry/wet mix, 0=dry and 1=full effect.

        The plugin may also have its own internal wet/dry control. Requires API 37+.
        """
        try:
            target = _target("effect", mixer_track, slot_index, expected_plugin)
            return get_connection().send_command(
                "pluginManagement.setMix", dict(target, value=value)
            )
        except (ValueError, RuntimeError, OSError) as exc:
            return _error(exc)

    @mcp.tool()
    def fl_set_plugin_enabled(
        kind: Kind,
        index: int,
        enabled: bool,
        slot_index: int = -1,
        expected_plugin: str | None = None,
    ) -> dict:
        """Enable/disable one instrument or bypass one effect without deleting it.

        Instruments use Channel mute. Effects use the visible standard green Mixer
        enable LED; an unreadable/unsupported theme fails before clicking.
        """
        try:
            target = _target(kind, index, slot_index, expected_plugin)
            if type(enabled) is not bool:
                raise ValueError("enabled must be boolean")
            conn = get_connection()
            with conn.transaction():
                if kind == "generator":
                    return conn.send_command(
                        "pluginManagement.setInstrumentEnabled", dict(target, enabled=enabled)
                    )
                return set_effect_enabled(conn, target, enabled)
        except (ValueError, RuntimeError, OSError) as exc:
            return _error(exc)

    @mcp.tool()
    def fl_delete_plugin(
        kind: Kind, index: int, expected_plugin: str, slot_index: int = -1, timeout: float = 8
    ) -> dict:
        """Delete exactly the named instance; verify other slots/channels afterward.

        Instrument deletion also removes that Channel's notes/automation in FL.
        Refresh inventory first; expected_plugin is the exact original plugin name.
        """
        try:
            target = _target(kind, index, slot_index, expected_plugin)
            if not 1 <= timeout <= 60:
                raise ValueError("timeout must be between 1 and 60 seconds")
            conn = get_connection()
            with conn.transaction():
                return delete_plugin(conn, target, timeout)
        except (ValueError, RuntimeError, OSError) as exc:
            return _error(exc)

    @mcp.tool()
    def fl_list_plugin_presets(query: str = "", plugin_name: str | None = None) -> dict:
        """List native .fst files from FL's user preset folders, without loading them.

        This lists saved files, not a VST's private preset browser. Companion
        manifests created by fl_save_plugin_preset identify the original plugin.
        """
        try:
            root = get_user_data_dir() / "Presets"
            result = []
            for folder in ("Plugin presets", "Channel presets"):
                for path in (root / folder).rglob("*.fst"):
                    if query.casefold() not in path.stem.casefold():
                        continue
                    associated = None
                    metadata = path.with_suffix(".fst.mcp.json")
                    if metadata.is_file():
                        try:
                            associated = json.loads(metadata.read_text(encoding="utf-8")).get(
                                "plugin_name"
                            )
                        except (OSError, ValueError):
                            pass
                    if plugin_name is not None and associated != plugin_name:
                        continue
                    result.append(
                        {
                            "name": path.stem,
                            "path": str(path),
                            "plugin_name": associated,
                            "folder": folder,
                        }
                    )
            return {
                "success": True,
                "presets": sorted(result, key=lambda p: p["path"].casefold()),
                "coverage": "user_FST_files",
                "private_vst_presets_included": False,
            }
        except (ValueError, RuntimeError, OSError) as exc:
            return _error(exc)

    @mcp.tool()
    def fl_save_plugin_preset(
        path: str,
        kind: Kind,
        index: int,
        slot_index: int = -1,
        expected_plugin: str | None = None,
        overwrite: bool = False,
        timeout: float = 20,
    ) -> dict:
        """Save complete native FST state to an explicit absolute destination.

        Uses FL's native Save preset as operation, not a parameter-only substitute.
        The file chunks and published source parameters are verified. Existing
        files are preserved unless overwrite=True. Parent directory must exist.
        """
        try:
            target = _target(kind, index, slot_index, expected_plugin)
            if not 2 <= timeout <= 180:
                raise ValueError("timeout must be between 2 and 180 seconds")
            conn = get_connection()
            with conn.transaction():
                return save_preset(conn, target, path, overwrite, timeout)
        except (ValueError, RuntimeError, OSError) as exc:
            return _error(exc)

    @mcp.tool()
    def fl_load_plugin_preset(
        path: str,
        kind: Kind,
        index: int,
        slot_index: int = -1,
        replace_existing: bool = False,
        expected_plugin: str | None = None,
        timeout: float = 60,
    ) -> dict:
        """Load complete FST into an explicit existing channel or effect slot.

        Effects may target any empty slot. Occupied targets require
        replace_existing=True. This does not create a new Channel Rack channel.
        Full hidden VST state is restored by FL's native FST loader, but only
        host-visible state can be independently verified. Keep FL untouched.
        """
        try:
            target = _target(kind, index, slot_index, expected_plugin)
            if not 5 <= timeout <= 180:
                raise ValueError("timeout must be between 5 and 180 seconds")
            conn = get_connection()
            with conn.transaction():
                return load_preset(
                    conn, target, path, get_user_data_dir(), replace_existing, timeout
                )
        except (ValueError, RuntimeError, OSError) as exc:
            return _error(exc)

    @mcp.tool()
    def fl_replace_plugin(
        plugin: str,
        kind: Kind,
        index: int,
        expected_plugin: str,
        slot_index: int = -1,
        timeout: float = 60,
    ) -> dict:
        """Replace an explicitly named current instance with a scanned catalog entry.

        Use fl_list_available_plugins for an exact VST/VST3 catalog ID and refresh
        inventory first. Notes/routing on an instrument Channel are checked for
        destination identity; private automation mappings may differ across plugins.
        """
        try:
            target = _target(kind, index, slot_index, expected_plugin)
            if not 5 <= timeout <= 180:
                raise ValueError("timeout must be between 5 and 180 seconds")
            entry = resolve_plugin(plugin, kind)
            path = get_user_data_dir() / "Presets" / "Plugin database" / entry["id"]
            conn = get_connection()
            with conn.transaction():
                current = call(conn, "snapshot", target)
                if not current["occupied"]:
                    raise ValueError(
                        "Use a preset load for an empty slot; replacement requires an instance"
                    )
                return load_preset(
                    conn,
                    target,
                    str(path),
                    get_user_data_dir(),
                    True,
                    timeout,
                    expected_loaded_plugin=entry["name"],
                )
        except (ValueError, RuntimeError, OSError) as exc:
            return _error(exc)

    @mcp.tool()
    def fl_move_effect(
        source_track: int,
        source_slot: int,
        destination_track: int,
        destination_slot: int,
        expected_plugin: str,
        allow_recreate: bool = False,
        timeout: float = 60,
    ) -> dict:
        """Move an effect to an explicit destination; same-track moves reorder natively.

        Cross-track moves save/load full FST then delete the source only after
        destination verification. They recreate the instance and do not preserve
        existing automation links, so allow_recreate=True is required. Cross-track
        destinations must be empty. Use fl_copy_effect to retain the source.
        """
        try:
            source = _target("effect", source_track, source_slot, expected_plugin)
            destination = _target("effect", destination_track, destination_slot)
            if not 5 <= timeout <= 180:
                raise ValueError("timeout must be between 5 and 180 seconds")
            conn = get_connection()
            with conn.transaction():
                if source_track == destination_track:
                    return move_effect_within_track(
                        conn, source_track, source_slot, destination_slot, expected_plugin
                    )
                if not allow_recreate:
                    raise ValueError(
                        "Cross-track moves recreate the instance; use allow_recreate=True"
                    )
                return _copy_effect(conn, source, destination, timeout, delete_source=True)
        except (ValueError, RuntimeError, OSError) as exc:
            return _error(exc)

    @mcp.tool()
    def fl_copy_effect(
        source_track: int,
        source_slot: int,
        destination_track: int,
        destination_slot: int,
        expected_plugin: str,
        timeout: float = 60,
    ) -> dict:
        """Copy complete FST state into an empty effect slot while retaining the source.

        Existing automation links remain on the source; links are not cloned.
        """
        try:
            source = _target("effect", source_track, source_slot, expected_plugin)
            destination = _target("effect", destination_track, destination_slot)
            if not 5 <= timeout <= 180:
                raise ValueError("timeout must be between 5 and 180 seconds")
            if source_track == destination_track and source_slot == destination_slot:
                raise ValueError("Source and destination must differ")
            conn = get_connection()
            with conn.transaction():
                return _copy_effect(conn, source, destination, timeout, delete_source=False)
        except (ValueError, RuntimeError, OSError) as exc:
            return _error(exc)


def _copy_effect(conn, source, destination, timeout, delete_source):
    original = call(
        conn,
        "snapshot",
        dict(source, parameter_limit=FULL_STATE_PARAMETER_LIMIT, include_names=True),
    )
    target = call(conn, "snapshot", destination)
    if target["occupied"]:
        raise ValueError("Effect copy/move destination must be empty")
    source_enabled = read_effect_enabled(conn, source)["enabled"]
    with tempfile.TemporaryDirectory(prefix="fl-mcp-preset-") as directory:
        path = Path(directory) / "state.fst"
        save_preset(conn, source, str(path), timeout=min(timeout, 30))
        result = load_preset(conn, destination, str(path), get_user_data_dir(), timeout=timeout)
        if not result.get("success") or result.get("status") != "loaded":
            return result
        loaded_target = dict(destination, expected_plugin=original["plugin_name"])
        try:
            if original.get("mix_level") is not None:
                call(conn, "setMix", dict(loaded_target, value=original["mix_level"]))
            set_effect_enabled(conn, loaded_target, source_enabled)
        except (ValueError, RuntimeError, OSError) as exc:
            return {
                "success": False,
                "status": "partial",
                "destination": result,
                "error": "Destination loaded but host slot state was not restored: " + str(exc),
                "source_retained": True,
            }
        current = call(
            conn,
            "snapshot",
            dict(source, parameter_limit=FULL_STATE_PARAMETER_LIMIT, include_names=True),
        )
        if identity(current) != identity(original) or (
            read_effect_enabled(conn, source)["enabled"] != source_enabled
        ):
            return {
                "success": False,
                "status": "partial",
                "error": "Destination loaded but source changed; it was retained",
                "destination": result,
            }
        if delete_source:
            try:
                deleted = delete_plugin(conn, source)
            except (ValueError, RuntimeError, OSError) as exc:
                return {
                    "success": False,
                    "status": "partial",
                    "destination": result,
                    "error": str(exc),
                    "source_retained_or_unknown": True,
                }
            return {
                "success": True,
                "status": "moved",
                "destination": result,
                "source_deletion": deleted,
                "automation_links_preserved": False,
                "method": "FST_copy_then_delete",
                "host_mix_and_bypass_preserved": True,
            }
        return {
            "success": True,
            "status": "copied",
            "destination": result,
            "source_retained": True,
            "automation_links_cloned": False,
            "host_mix_and_bypass_preserved": True,
        }
