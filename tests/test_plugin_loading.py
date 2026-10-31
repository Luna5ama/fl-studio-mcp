"""Exercise loading state and mailbox behavior without a DAW or real VST binaries."""

import asyncio
import importlib.util
import json
import sys
import threading
import time
import types
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest

from fl_studio_mcp.utils.plugin_catalog import list_plugins, resolve_plugin

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("FL_STUDIO_USER_DATA_DIR", str(tmp_path))
    root = tmp_path / "Presets" / "Plugin database"
    for relative in (
        "Installed/Generators/VST3/Synth.fst",
        "Installed/Generators/VST/Synth.fst",
        "Installed/Effects/VST3/Effect.fst",
        "Generators/Favorites/Synth.fst",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test fixture")
    return root


def test_catalog_uses_scanned_plugins_and_keeps_formats(database):
    entries = list_plugins("sYnTh", "generator")
    assert [entry["format"] for entry in entries] == ["VST", "VST3"]
    assert all(entry["id"].startswith("Installed/") for entry in entries)


def test_ambiguous_names_require_exact_catalog_id(database):
    with pytest.raises(ValueError, match="Ambiguous"):
        resolve_plugin("Synth", "generator")
    entry = resolve_plugin("Installed/Generators/VST3/Synth.fst", "generator")
    assert entry["format"] == "VST3"
    with pytest.raises(ValueError, match="not found"):
        resolve_plugin(entry["id"], "effect")


def test_missing_database_is_actionable(tmp_path):
    with pytest.raises(ValueError, match="Scan plugins"):
        list_plugins(database=tmp_path / "missing")


class Engine:
    def __init__(self, database):
        self.database = database
        self.channels = ["Sampler"]
        self.effects = [[None] * 10 for _ in range(4)]
        self.track = 0
        self.auto_hide = 1
        self.actions = []
        self.pending = []
        self.menu = False
        self.menu_index = -1
        self.expanded = set()
        self.selected = "Current project"
        self.parent = {}
        self.children = {"": ["Current project", "Plugin database", "Packs"]}
        self.types = {"Current project": -101, "Plugin database": -1000, "Packs": -100}
        for path in sorted(database.rglob("*")):
            relative = path.relative_to(database).as_posix()
            key = "Plugin database/" + relative
            parent = key.rsplit("/", 1)[0]
            self.children.setdefault(parent, []).append(key)
            self.parent[key] = parent
            self.types[key] = -100 if path.is_dir() else 4

    def visible(self, parent=""):
        result = []
        for node in self.children.get(parent, []):
            result.append(node)
            if node in self.expanded:
                result.extend(self.visible(node))
        return result

    def getFocusedNodeCaption(self):
        return self.selected.rsplit("/", 1)[-1]

    def getFocusedNodeFileType(self):
        return self.types[self.selected]

    def toggleBrowserNode(self, value):
        if value:
            self.expanded.add(self.selected)
        else:
            self.expanded.discard(self.selected)

    def navigateBrowser(self, direction, shift):
        assert shift == 0
        if self.menu:
            self.menu_index = min(self.menu_index + 1, len(self.menu_entries()) - 1)
            return self.menu_entries()[self.menu_index]
        visible = self.visible()
        index = visible.index(self.selected)
        index = max(0, min(len(visible) - 1, index + (1 if direction == 41 else -1)))
        self.selected = visible[index]
        return self.getFocusedNodeCaption()

    def menu_entries(self):
        entries = ["Send to selected channel or focused plugin"]
        if "/Generators/" in self.selected:
            entries.append("Open in new channel")
        return entries + ["Delete file"]

    def selectBrowserMenuItem(self):
        action = self.menu_entries()[self.menu_index]
        self.actions.append(action)
        self.menu = False
        name = Path(self.selected).stem
        if action == "Open in new channel":
            self.channels.append(name)
        elif action == "Send to selected channel or focused plugin":
            slot = self.effects[self.track].index(None)
            self.effects[self.track][slot] = name
        else:
            raise AssertionError("Loading must never select an unrelated menu action")

    def focus(self, window):
        # FL can restore the old Mixer track when a queued focus change is applied.
        self.pending.append(lambda: setattr(self, "track", 0))

    def flush(self):
        pending, self.pending = self.pending, []
        for action in pending:
            action()


@pytest.fixture
def controller(database, monkeypatch):
    engine = Engine(database)
    modules = {
        "channels": types.SimpleNamespace(
            channelCount=lambda global_index: len(engine.channels),
            channelNumber=lambda global_index: len(engine.channels) - 1,
            setTargetFxTrack=lambda *args: engine.actions.append(("route", args)),
        ),
        "midi": types.SimpleNamespace(
            widBrowser=4,
            widMixer=0,
            FPT_Up=40,
            FPT_Down=41,
            FPT_ItemMenu=91,
            PME_System=1,
            PME_FromMIDI=32,
        ),
        "mixer": types.SimpleNamespace(
            trackCount=lambda: 5,
            trackNumber=lambda: engine.track,
            setActiveTrack=lambda track: setattr(engine, "track", track),
        ),
        "plugins": types.SimpleNamespace(
            isValid=lambda index, slot, global_index: (
                index > 0 and index < len(engine.channels)
                if slot == -1
                else engine.effects[index][slot] is not None
            ),
            getPluginName=lambda index, slot, user_name, global_index: (
                engine.channels[index] if slot == -1 else engine.effects[index][slot]
            ),
        ),
        "ui": types.SimpleNamespace(
            getFocusedNodeCaption=engine.getFocusedNodeCaption,
            getFocusedNodeFileType=engine.getFocusedNodeFileType,
            toggleBrowserNode=engine.toggleBrowserNode,
            navigateBrowser=engine.navigateBrowser,
            navigateBrowserTabs=lambda value: None,
            showWindow=engine.focus,
            setFocused=engine.focus,
            getFocused=lambda window: True,
            isBrowserAutoHide=lambda: engine.auto_hide,
            setBrowserAutoHide=lambda value: setattr(engine, "auto_hide", value),
            isInPopupMenu=lambda: engine.menu,
            closeActivePopupMenu=lambda: setattr(engine, "menu", False),
            selectBrowserMenuItem=engine.selectBrowserMenuItem,
        ),
        "transport": types.SimpleNamespace(
            globalTransport=lambda *args: engine.pending.append(
                lambda: setattr(engine, "menu", True)
            ),
        ),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    module = types.ModuleType("controller_under_test")
    source = ROOT / "fl_controller" / "device_FLStudioMCP.py"
    exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), module.__dict__)
    return module, engine


def activate_fake_destination(module, engine):
    state = module._plugin_load
    if state.get("stage") not in ("awaiting_menu", "awaiting_drag"):
        return
    module.dispatch_command("plugins.confirmLoadMenu", {"operation_id": state["operation_id"]})
    if state["kind"] == "generator":
        engine.menu_index = engine.menu_entries().index("Open in new channel")
        engine.selectBrowserMenuItem()
    else:
        slot = state["destination_slot"]
        engine.effects[engine.track][slot] = Path(engine.selected).stem
        engine.actions.append(("drag", engine.track, slot))


def finish(module, engine):
    for _ in range(500):
        engine.flush()
        module.OnIdle()
        activate_fake_destination(module, engine)
        if module._plugin_load["status"] != "loading":
            return module._plugin_load
    pytest.fail("Loading did not reach a terminal state")


@pytest.mark.parametrize(
    "kind,plugin,track",
    [
        ("generator", "Installed/Generators/VST3/Synth.fst", None),
        ("effect", "Installed/Effects/VST3/Effect.fst", 2),
    ],
)
def test_loading_uses_exact_preset_and_verifies_destination(controller, kind, plugin, track):
    module, engine = controller
    engine.effects[2][0] = "Existing effect"
    result = module.dispatch_command(
        "plugins.beginLoad",
        {
            "plugin_id": plugin,
            "kind": kind,
            "mixer_track": track,
        },
    )
    with pytest.raises(RuntimeError, match="loading"):
        module.dispatch_command("transport.start", {})
    status = finish(module, engine)
    public = module.dispatch_command("plugins.getLoadStatus", result)
    assert public["status"] == status["status"] == "loaded"
    assert not any(key.startswith("_") for key in public)
    if kind == "generator":
        assert public["channel_index"] == 1
        assert engine.channels == ["Sampler", "Synth"]
        assert engine.actions == ["Open in new channel"]
    else:
        assert (public["mixer_track"], public["slot_index"]) == (2, 1)
        assert engine.effects[2][:2] == ["Existing effect", "Effect"]
        assert engine.effects[0] == [None] * 10


@pytest.mark.parametrize(
    "plugin,kind",
    [
        ("../outside.fst", "generator"),
        ("Installed/Generators/../../outside.fst", "generator"),
        ("Installed/Effects/VST3/Effect.fst", "generator"),
        ("Installed/Generators/VST3/absent.fst", "generator"),
    ],
)
def test_invalid_requests_have_no_ui_side_effect(controller, plugin, kind):
    module, engine = controller
    with pytest.raises(ValueError):
        module.begin_plugin_load({"plugin_id": plugin, "kind": kind})
    assert engine.actions == engine.pending == []


def test_full_effect_track_is_rejected_without_navigation(controller):
    module, engine = controller
    engine.effects[1] = ["Existing"] * 10
    with pytest.raises(ValueError, match="no empty"):
        module.begin_plugin_load(
            {
                "plugin_id": "Installed/Effects/VST3/Effect.fst",
                "kind": "effect",
                "mixer_track": 1,
            }
        )
    assert engine.pending == []


def test_timeout_before_activation_does_not_add_a_plugin(controller):
    module, engine = controller
    module.begin_plugin_load(
        {
            "plugin_id": "Installed/Generators/VST3/Synth.fst",
            "kind": "generator",
        }
    )
    module._plugin_load["_deadline"] = time.monotonic() - 1
    module.OnIdle()
    assert module._plugin_load["status"] == "failed"
    assert engine.actions == []


def test_browser_drift_fails_without_loading_another_file(controller):
    module, engine = controller
    # Simulate a filtered Browser that hides the requested plugin.
    parent = "Plugin database/Installed/Generators/VST3"
    engine.children[parent] = []
    module.begin_plugin_load(
        {
            "plugin_id": "Installed/Generators/VST3/Synth.fst",
            "kind": "generator",
        }
    )
    status = finish(module, engine)
    assert status["status"] == "failed"
    assert engine.actions == []


def test_stale_operation_cannot_observe_an_unrelated_load(controller):
    module, _ = controller
    with pytest.raises(ValueError, match="Unknown"):
        module.dispatch_command("plugins.getLoadStatus", {"operation_id": "old"})


def test_effect_target_drift_aborts_before_activating_menu(controller):
    module, engine = controller
    module.begin_plugin_load(
        {
            "plugin_id": "Installed/Effects/VST3/Effect.fst",
            "kind": "effect",
            "mixer_track": 2,
        }
    )
    for _ in range(500):
        engine.flush()
        module.OnIdle()
        if module._plugin_load.get("stage") == "awaiting_drag":
            break
    engine.track = 0
    operation = {"operation_id": module._plugin_load["operation_id"]}
    with pytest.raises(RuntimeError, match="Mixer target changed"):
        module.dispatch_command("plugins.confirmLoadMenu", operation)
    module.dispatch_command("plugins.cancelLoad", operation)
    assert module._plugin_load["status"] == "cancelled"
    assert engine.actions == []
    assert not engine.menu


def test_an_empty_wrapper_is_not_reported_as_a_loaded_vst(controller):
    module, engine = controller
    module.begin_plugin_load(
        {
            "plugin_id": "Installed/Generators/VST3/Synth.fst",
            "kind": "generator",
        }
    )
    for _ in range(500):
        engine.flush()
        module.OnIdle()
        activate_fake_destination(module, engine)
        if engine.actions:
            engine.channels[-1] = "Fruity Wrapper"
            break
    module.OnIdle()
    assert module._plugin_load["status"] == "loading"
    module._plugin_load["_deadline"] = time.monotonic() - 1
    module.OnIdle()
    assert module._plugin_load["status"] == "unknown"


def test_status_polling_can_progress_when_idle_callbacks_are_unavailable(controller):
    module, engine = controller
    result = module.begin_plugin_load(
        {
            "plugin_id": "Installed/Generators/VST3/Synth.fst",
            "kind": "generator",
        }
    )
    for _ in range(500):
        engine.flush()
        status = module.dispatch_command("plugins.getLoadStatus", result)
        activate_fake_destination(module, engine)
        if status["status"] != "loading":
            break
    assert status["status"] == "loaded"
    assert engine.channels == ["Sampler", "Synth"]
    assert engine.auto_hide == 1


def test_nested_idle_during_a_ui_call_cannot_reenter_the_generator(controller, monkeypatch):
    module, engine = controller
    original = module.ui.setFocused

    def reentrant_focus(window):
        module.OnIdle()
        original(window)

    monkeypatch.setattr(module.ui, "setFocused", reentrant_focus)
    module.begin_plugin_load(
        {
            "plugin_id": "Installed/Generators/VST3/Synth.fst",
            "kind": "generator",
        }
    )
    assert finish(module, engine)["status"] == "loaded"


def test_response_writing_works_without_sandbox_forbidden_rename(controller, tmp_path, monkeypatch):
    module, _ = controller
    module.RESPONSE_FILE = tmp_path / "response.json"

    def forbidden(*args):
        raise PermissionError("FL sandbox forbids rename")

    monkeypatch.setattr(Path, "replace", forbidden)
    module.write_response({"success": True, "name": "音源"})
    assert json.loads(module.RESPONSE_FILE.read_text(encoding="utf-8"))["name"] == "音源"


def test_slot_ocr_normalization_keeps_destination_numbers_exact():
    from fl_studio_mcp.utils.windows_plugin_menu import _normalize

    assert _normalize("510t1") == _normalize("Slot 1")
    assert _normalize("5(ot5") == _normalize("Slot 5")
    assert _normalize("Slot 10") != _normalize("Slot 1")
    assert _normalize("Other Slot 1") != _normalize("Slot 1")


def test_native_activation_failure_is_never_retried(database, monkeypatch):
    from fl_studio_mcp.tools import plugin_loading

    functions = {}
    actions = []

    class Registry:
        def tool(self):
            return lambda function: functions.setdefault(function.__name__, function)

    class Connection:
        def transaction(self):
            from contextlib import nullcontext

            return nullcontext()

        def send_command(self, action, params):
            actions.append(action)
            return {
                "success": True,
                "operation_id": "one",
                "status": "loading",
                "stage": "awaiting_menu",
                "menu_action": "open in new channel",
            }

    def fail_activation(action, confirm):
        confirm()
        raise OSError("Desktop changed")

    monkeypatch.setattr(plugin_loading, "sys", types.SimpleNamespace(platform="win32"))
    monkeypatch.setattr(plugin_loading, "get_connection", Connection)
    monkeypatch.setattr(plugin_loading, "select_plugin_menu_action", fail_activation)
    monkeypatch.setattr(plugin_loading, "FLWindows", nullcontext)
    plugin_loading.register_plugin_loading_tools(Registry())
    result = functions["fl_add_plugin"]("Installed/Generators/VST3/Synth.fst", "generator")
    assert not result["success"]
    assert actions == [
        "plugins.beginLoad",
        "plugins.getLoadStatus",
        "plugins.confirmLoadMenu",
        "plugins.cancelLoad",
    ]


def test_duplicate_midi_trigger_does_not_repeat_a_mutation(controller, tmp_path):
    module, engine = controller
    module.COMMAND_FILE = tmp_path / "command.json"
    module.RESPONSE_FILE = tmp_path / "response.json"
    module.dispatch_command = lambda action, params: engine.actions.append(action) or {}
    module.COMMAND_FILE.write_text(json.dumps({"action": "mutate", "request_id": "one"}))
    module.execute_pending_command()
    module.execute_pending_command()
    assert engine.actions == ["mutate"]
    assert json.loads(module.RESPONSE_FILE.read_text())["request_id"] == "one"


def test_midi_mailbox_serializes_parallel_commands_and_preserves_unicode(database):
    from fl_studio_mcp.utils.midi_connection import MIDIConnection

    connection = MIDIConnection()
    received = []

    class Port:
        def send(self, _):
            command = json.loads(connection._command_file.read_text(encoding="utf-8"))
            received.append(command)
            time.sleep(0.01)
            connection._response_file.write_text(
                json.dumps(
                    {
                        "success": True,
                        "request_id": command["request_id"],
                        "value": command["params"]["value"],
                    }
                ),
                encoding="utf-8",
            )

    connection._port = Port()
    connection._connected = True
    results = []
    threads = [
        threading.Thread(
            target=lambda value=value: results.append(
                connection.send_command("test", {"value": value})
            )
        )
        for value in ("音源", "效果")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert sorted(item["value"] for item in results) == ["效果", "音源"]
    assert len({command["request_id"] for command in received}) == 2


def test_mcp_discovery_and_invalid_load_do_not_require_a_midi_port(database):
    from fastmcp import Client

    from fl_studio_mcp.server import mcp

    async def check():
        async with Client(mcp) as client:
            tools = {tool.name for tool in await client.list_tools()}
            assert {
                "fl_list_available_plugins",
                "fl_add_plugin",
                "fl_get_plugin_load_status",
            } <= tools
            result = await client.call_tool("fl_list_available_plugins", {"kind": "effect"})
            assert result.data["success"] is True
            assert result.data["plugins"][0]["name"] == "Effect"
            result = await client.call_tool("fl_add_plugin", {"plugin": "Effect", "kind": "effect"})
            assert result.data["success"] is False
            assert "mixer_track" in result.data["error"]

    asyncio.run(check())


def test_installer_bakes_relocated_path_and_backs_up_existing_controller(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "installer", ROOT / "scripts/install_controller.py"
    )
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    user_data = tmp_path / "自定义路径" / "FL Studio"
    destination = installer.install_controller(user_data)
    source = destination.read_text(encoding="utf-8")
    assert f"USER_DATA_DIR = {str(user_data.resolve())!r}" in source
    assert "import winreg" not in source
    installer.install_controller(user_data)
    backups = list(destination.parent.glob("*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == source


@pytest.mark.parametrize("activation_succeeds", [True, False])
def test_public_add_activates_fl_inside_transaction_before_begin_load(
    database, monkeypatch, activation_succeeds
):
    from fl_studio_mcp.tools import plugin_loading
    from fl_studio_mcp.utils.runtime_operations import LoadOperations

    functions = {}
    events = []
    state = {"locked": False, "activated": False}

    class Registry:
        def tool(self):
            return lambda function: functions.setdefault(function.__name__, function)

    class Connection:
        @contextmanager
        def transaction(self):
            events.append("transaction_enter")
            state["locked"] = True
            try:
                yield
            finally:
                assert state["locked"]
                state["locked"] = False
                events.append("transaction_exit")

        def send_command(self, action, params):
            assert state["locked"] and state["activated"]
            events.append(action)
            if action == "plugins.beginLoad":
                return {
                    "success": True,
                    "operation_id": "activated-public-load",
                    "status": "loading",
                }
            assert action == "plugins.getLoadStatus"
            return {"success": True, "operation_id": "activated-public-load", "status": "loaded"}

    class Windows:
        def __enter__(self):
            assert state["locked"]
            events.append("activate")
            if not activation_succeeds:
                raise RuntimeError("Foreground activation refused")
            state["activated"] = True
            return self

        def __exit__(self, *args):
            assert state["locked"]
            events.append("window_context_exit")
            state["activated"] = False

    monkeypatch.setattr(plugin_loading, "sys", types.SimpleNamespace(platform="win32"))
    monkeypatch.setattr(plugin_loading, "get_connection", Connection)
    monkeypatch.setattr(plugin_loading, "FLWindows", Windows)
    monkeypatch.setattr(plugin_loading, "load_operations", LoadOperations())
    monkeypatch.setattr(plugin_loading.time, "sleep", lambda seconds: None)
    plugin_loading.register_plugin_loading_tools(Registry())
    result = functions["fl_add_plugin"]("Installed/Generators/VST3/Synth.fst", "generator")
    assert state["locked"] is False and state["activated"] is False
    if activation_succeeds:
        assert result["status"] == "loaded" and result["success"]
        assert events == [
            "transaction_enter",
            "activate",
            "plugins.beginLoad",
            "plugins.getLoadStatus",
            "window_context_exit",
            "transaction_exit",
        ]
    else:
        assert result["success"] is False and "Foreground activation refused" in result["error"]
        assert events == ["transaction_enter", "activate", "transaction_exit"]
        assert plugin_loading.load_operations.list() == []
