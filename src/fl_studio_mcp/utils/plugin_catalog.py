"""Discover presets created by FL Studio's Plugin Manager, without scanning DLLs."""

from __future__ import annotations

from pathlib import Path

from fl_studio_mcp.utils.fl_paths import get_plugin_database_dir


def list_plugins(query: str = "", kind: str = "all", database: Path | None = None) -> list[dict]:
    if kind not in ("all", "generator", "effect"):
        raise ValueError("kind must be all, generator, or effect")
    root = database if database is not None else get_plugin_database_dir()
    if not root.is_dir():
        raise ValueError(f"Plugin database not found: {root}. Scan plugins in FL Studio first.")
    entries = []
    # Installed entries identify the plugin format; favorites can be arbitrary presets.
    for folder, entry_kind in (("Generators", "generator"), ("Effects", "effect")):
        if kind not in ("all", entry_kind):
            continue
        for path in (root / "Installed" / folder).rglob("*.fst"):
            if query.casefold() not in path.stem.casefold():
                continue
            relative = path.relative_to(root)
            entries.append(
                {
                    "id": relative.as_posix(),
                    "name": path.stem,
                    "kind": entry_kind,
                    "format": relative.parts[2] if len(relative.parts) > 3 else "unknown",
                }
            )
    return sorted(entries, key=lambda entry: entry["id"].casefold())


def resolve_plugin(plugin: str, kind: str) -> dict:
    if not plugin.strip():
        raise ValueError("plugin must be a catalog id or an exact plugin name")
    entries = list_plugins(kind=kind)
    matches = [entry for entry in entries if entry["id"].casefold() == plugin.casefold()]
    if not matches:
        matches = [entry for entry in entries if entry["name"].casefold() == plugin.casefold()]
    if not matches:
        raise ValueError(f"Plugin not found: {plugin}. Use fl_list_available_plugins first.")
    if len(matches) != 1:
        choices = ", ".join(entry["id"] for entry in matches)
        raise ValueError(f"Ambiguous plugin name; pass an exact catalog id: {choices}")
    return matches[0]
