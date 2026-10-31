"""Locate FL Studio's user data, including relocated Windows installations."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def get_user_data_dir() -> Path:
    """Return the FL Studio folder shown in Options > File settings."""
    override = os.environ.get("FL_STUDIO_USER_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Software\Image-Line\Shared\Paths"
            ) as key:
                shared = winreg.QueryValueEx(key, "Shared data")[0]
                return Path(shared) / "FL Studio"
        except OSError:
            pass
    return Path.home() / "Documents" / "Image-Line" / "FL Studio"


def get_plugin_database_dir() -> Path:
    return get_user_data_dir() / "Presets" / "Plugin database"
