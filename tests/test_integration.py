"""Check public registration and controller seams without native input."""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import mido
import pytest
from test_plugin_loading import controller as controller
from test_plugin_loading import database as database

ROOT = Path(__file__).resolve().parents[1]


def test_all_new_tool_categories_are_registered():
    from fl_studio_mcp.server import mcp

    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert {
        "fl_add_plugin",
        "fl_get_project_plugins",
        "fl_save_plugin_preset",
        "fl_move_effect",
        "fl_create_pattern",
        "fl_place_pattern_clip",
        "fl_route_to_bus",
        "fl_sidechain_mixer_track",
        "fl_import_sample",
        "fl_save_project",
        "fl_export_wav",
        "fl_check_environment",
        "fl_cancel_plugin_load",
    } <= names


@pytest.mark.parametrize("ports", [["Microsoft GS Wavetable Synth 0"], ["FL one", "FL two"]])
def test_midi_does_not_guess_an_unrelated_or_ambiguous_port(tmp_path, monkeypatch, ports):
    from fl_studio_mcp.utils.midi_connection import MIDIConnection

    monkeypatch.setenv("FL_STUDIO_USER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("FL_STUDIO_MIDI_PORT", raising=False)
    monkeypatch.setattr(mido, "get_output_names", lambda: ports)
    monkeypatch.setattr(mido, "open_output", lambda name: pytest.fail("must not open " + name))
    connection = MIDIConnection()
    assert connection.connect() is False
    assert "FL_STUDIO_MIDI_PORT" in connection.connection_error


def test_explicit_midi_port_can_select_an_otherwise_ambiguous_device(tmp_path, monkeypatch):
    from fl_studio_mcp.utils.midi_connection import MIDIConnection

    monkeypatch.setenv("FL_STUDIO_USER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FL_STUDIO_MIDI_PORT", "FL two")
    monkeypatch.setattr(mido, "get_output_names", lambda: ["FL one", "FL two"])
    port = object()
    monkeypatch.setattr(mido, "open_output", lambda name: port if name == "FL two" else None)
    connection = MIDIConnection()
    assert connection.connect() is True
    assert connection.get_status()["port_name"] == "FL two"


def test_addon_busy_guard_prevents_another_native_owner(controller):
    module, _ = controller
    addon = types.SimpleNamespace(is_busy=lambda: True, dispatch=lambda *args: None)
    module._addons = {"mcp_plugin_management": addon}
    with pytest.raises(RuntimeError, match="active"):
        module.dispatch_command("plugins.beginLoad", {})
    with pytest.raises(RuntimeError, match="active"):
        module.dispatch_command("arrangement.createPattern", {})


def test_idle_is_reentrant_safe_and_addon_failure_is_isolated(controller):
    module, _ = controller
    calls = []
    module.advance_plugin_load = lambda: calls.append("loader")

    def broken_idle():
        calls.append("broken")
        module.OnIdle()
        raise RuntimeError("fixture failure")

    module._addons = {
        "broken": types.SimpleNamespace(on_idle=broken_idle),
        "next": types.SimpleNamespace(on_idle=lambda: calls.append("next")),
    }
    module.OnIdle()
    assert calls == ["loader", "broken", "next"]
    assert "fixture failure" in module._addon_errors["broken"]
    assert module._idle_running is False


def test_project_summary_uses_bpm_and_distinguishes_bar_ticks(monkeypatch):
    monkeypatch.setitem(sys.modules, "channels", types.SimpleNamespace(channelCount=lambda _: 1))
    monkeypatch.setitem(
        sys.modules,
        "general",
        types.SimpleNamespace(
            getChangedFlag=lambda: 0, getRecPPQ=lambda: 96, getRecPPB=lambda: 384
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "mixer",
        types.SimpleNamespace(
            trackCount=lambda: 18,
            trackNumber=lambda: 0,
            getCurrentTempo=lambda as_int: 140000 if as_int else 140.0,
        ),
    )
    monkeypatch.setitem(sys.modules, "ui", types.SimpleNamespace())
    spec = importlib.util.spec_from_file_location(
        "summary_fixture", ROOT / "fl_controller/mcp_system.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.dispatch("system.getProjectSummary", {})
    assert result["tempo"] == 140.0
    assert result["beats_per_bar"] == 4
    assert result["ticks_per_bar"] == 384


def test_controller_unload_restores_only_owned_temporary_browser_state(controller):
    module, engine = controller
    module._addons = {}
    module._plugin_load = {
        "status": "loading",
        "_browser_auto_hide": 1,
        "_issued": False,
        "_menu_opened": True,
    }
    engine.auto_hide = 0
    engine.menu = True
    module.OnDeInit()
    assert engine.auto_hide == 1
    assert engine.menu is False
    # Once restored, a later cleanup must not overwrite a user preference.
    engine.auto_hide = 0
    module.OnDeInit()
    assert engine.auto_hide == 0


def test_unload_isolates_addon_cleanup_failures(controller):
    module, _ = controller
    calls = []

    def broken():
        calls.append("broken")
        raise RuntimeError("fixture cleanup")

    module._addons = {
        "one": types.SimpleNamespace(on_deinit=broken),
        "two": types.SimpleNamespace(on_deinit=lambda: calls.append("next")),
    }
    module.OnDeInit()
    assert calls == ["broken", "next"]


def test_installer_bakes_matching_controller_and_piano_roll_paths(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "installer_seam", ROOT / "scripts/install_controller.py"
    )
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    user_data = tmp_path / "moved data" / "FL Studio"
    controller_path = installer.install_controller(user_data)
    piano_path = installer.install_piano_roll(user_data)
    baked = f"USER_DATA_DIR = {str(user_data.resolve())!r}"
    assert baked in controller_path.read_text(encoding="utf-8")
    assert baked in piano_path.read_text(encoding="utf-8")
    helpers = {path.name for path in (ROOT / "fl_controller").glob("mcp_*.py")}
    assert helpers <= {path.name for path in controller_path.parent.glob("mcp_*.py")}
    piano_path.write_text("previous user script", encoding="utf-8")
    installer.install_piano_roll(user_data)
    backups = list(piano_path.parent.glob("*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "previous user script"


def test_window_ownership_check_rounds_inferred_control_centers():
    from fl_studio_mcp.utils.windows_ui import FLWindows

    seen = []

    class Function:
        def __init__(self, callback):
            self.callback = callback

        def __call__(self, *args):
            return self.callback(*args)

    window = FLWindows.__new__(FLWindows)
    window.user = types.SimpleNamespace(
        WindowFromPoint=Function(lambda point: seen.append((point.x, point.y)) or 12),
        IsChild=Function(lambda parent, child: False),
    )
    window._check_point(12, (3.25, 4.75))
    assert seen == [(3, 5)]


@pytest.mark.parametrize(
    "false_value,true_value,expected",
    [(5000, 5.0, 5.0), (2088000, 2088.0, 2088.0), (140.125, 140, 140.125), (70000.0, 70.0, 70.0)],
)
def test_tempo_readback_handles_meter_scaled_ui_ranges(
    monkeypatch, false_value, true_value, expected
):
    for name in ("channels", "general", "ui"):
        monkeypatch.setitem(sys.modules, name, types.SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "mixer",
        types.SimpleNamespace(getCurrentTempo=lambda mode: true_value if mode else false_value),
    )
    spec = importlib.util.spec_from_file_location(
        "tempo_meter_fixture", ROOT / "fl_controller/mcp_system.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._tempo_bpm() == expected
