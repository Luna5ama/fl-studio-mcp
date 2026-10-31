"""Install the controller into FL Studio's actual user data directory.

Run with ``uv run python scripts/install_controller.py`` after ``uv sync``.
FL's embedded Python restricts registry access, so resolve relocation outside FL.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from fl_studio_mcp.utils.fl_paths import get_user_data_dir


def install_controller(user_data: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "fl_controller" / "device_FLStudioMCP.py"
    destination = user_data / "Settings" / "Hardware" / "FLStudioMCP" / source.name
    content = source.read_text(encoding="utf-8").replace(
        "USER_DATA_DIR = None", f"USER_DATA_DIR = {str(user_data.resolve())!r}", 1
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        destination.with_suffix(f".py.{stamp}.bak").write_bytes(destination.read_bytes())
    destination.write_text(content, encoding="utf-8")
    for helper in source.parent.glob("mcp_*.py"):
        target = destination.parent / helper.name
        data = helper.read_bytes()
        if target.exists() and target.read_bytes() != data:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            target.with_suffix(f".py.{stamp}.bak").write_bytes(target.read_bytes())
        target.write_bytes(data)
    return destination


def install_piano_roll(user_data: Path) -> Path:
    """Install the Piano Roll script with the same resolved data path as MIDI."""
    source = Path(__file__).resolve().parent / "ComposeWithLLM.pyscript"
    destination = user_data / "Settings" / "Piano roll scripts" / source.name
    content = source.read_text(encoding="utf-8").replace(
        "USER_DATA_DIR = None", f"USER_DATA_DIR = {str(user_data.resolve())!r}", 1
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.read_text(encoding="utf-8") != content:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        destination.with_suffix(f".pyscript.{stamp}.bak").write_bytes(destination.read_bytes())
    destination.write_text(content, encoding="utf-8")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-data-dir", type=Path)
    args = parser.parse_args()
    user_data = args.user_data_dir or get_user_data_dir()
    print(install_controller(user_data))
    print(install_piano_roll(user_data))
    print("Reload the FLStudioMCP controller in FL Studio MIDI Settings after installing.")
