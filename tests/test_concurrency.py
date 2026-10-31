"""Verify connection lifecycle and Piano Roll share native-operation ownership."""

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from fl_studio_mcp.tools import piano_roll
from fl_studio_mcp.utils import fl_trigger, midi_connection


@pytest.fixture
def request_file(monkeypatch, tmp_path):
    path = tmp_path / "mcp_request.json"
    monkeypatch.setattr(piano_roll, "_get_request_file", lambda: path)
    monkeypatch.setattr(piano_roll, "_get_response_file", lambda: tmp_path / "mcp_response.json")
    monkeypatch.setattr(piano_roll, "_pending_script", None)
    return path


def _registered_tools():
    functions = {}

    class Registry:
        def tool(self):
            return lambda function: functions.setdefault(function.__name__, function)

    piano_roll.register_piano_roll_tools(Registry())
    return functions


@pytest.mark.parametrize("action", ("connect", "disconnect", "reset"))
def test_connection_lifecycle_waits_for_native_transaction(monkeypatch, action):
    connection = object.__new__(midi_connection.MIDIConnection)
    changed = threading.Event()
    entered = threading.Event()
    finished = threading.Event()
    port = SimpleNamespace(close=changed.set)
    connection._command_lock = midi_connection._mailbox_lock
    connection._port = None if action == "connect" else port
    connection._port_name = None
    connection._connected = action != "connect"
    connection._error = None
    monkeypatch.setattr(midi_connection, "_connection", connection)
    monkeypatch.setenv("FL_STUDIO_MIDI_PORT", "test virtual port")
    monkeypatch.setitem(
        sys.modules,
        "mido",
        SimpleNamespace(
            get_output_names=lambda: changed.set() or ["test virtual port"],
            open_output=lambda name: port,
        ),
    )

    def run():
        entered.set()
        try:
            if action == "reset":
                midi_connection.reset_connection()
            else:
                getattr(connection, action)()
        finally:
            finished.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with connection.transaction():
            future = executor.submit(run)
            assert entered.wait(1)
            assert not finished.wait(0.05)
            assert not changed.is_set()
        future.result(timeout=1)
    assert changed.is_set()
    if action == "reset":
        assert midi_connection._connection is None


def test_queue_waits_for_other_native_operations(monkeypatch, request_file):
    tools = _registered_tools()
    entered = threading.Event()

    def queue():
        entered.set()
        return tools["fl_send_notes"]([{"midi": 60, "duration": 1}], auto_trigger=False)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with midi_connection._mailbox_lock:
            future = executor.submit(queue)
            assert entered.wait(1)
            assert not future.done()
            assert not request_file.exists()
        future.result(timeout=1)
    assert json.loads(request_file.read_text())[0]["notes"][0]["midi"] == 60


def test_queue_only_call_cannot_be_consumed_by_an_inflight_trigger(monkeypatch, request_file):
    tools = _registered_tools()
    executing = threading.Event()
    release = threading.Event()
    queued = threading.Event()
    consumed = []

    def trigger(auto_trigger):
        if not auto_trigger:
            return ""
        consumed.extend(json.loads(request_file.read_text()))
        executing.set()
        assert release.wait(2)
        request_file.unlink()
        return " triggered"

    monkeypatch.setattr(piano_roll, "_get_trigger_info", trigger)

    def queue_only():
        queued.set()
        return tools["fl_send_notes"]([{"midi": 64, "duration": 1}], auto_trigger=False)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(tools["fl_send_notes"], [{"midi": 60, "duration": 1}])
        try:
            assert executing.wait(1)
            second = executor.submit(queue_only)
            assert queued.wait(1)
            assert not second.done()
            assert json.loads(request_file.read_text())[0]["notes"][0]["midi"] == 60
        finally:
            release.set()
        first.result(timeout=1)
        second.result(timeout=1)
    assert consumed[0]["notes"][0]["midi"] == 60
    remaining = json.loads(request_file.read_text())
    assert len(remaining) == 1
    assert remaining[0]["notes"][0]["midi"] == 64


def test_clear_queue_waits_until_script_finishes(monkeypatch, request_file):
    tools = _registered_tools()
    executing = threading.Event()
    release = threading.Event()
    clearing = threading.Event()

    def trigger(auto_trigger):
        executing.set()
        assert release.wait(2)
        assert request_file.exists()
        return " triggered"

    def clear():
        clearing.set()
        return tools["fl_clear_request_queue"]()

    monkeypatch.setattr(piano_roll, "_get_trigger_info", trigger)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(tools["fl_clear_piano_roll"])
        try:
            assert executing.wait(1)
            second = executor.submit(clear)
            assert clearing.wait(1)
            assert not second.done()
            assert request_file.exists()
        finally:
            release.set()
        first.result(timeout=1)
        second.result(timeout=1)
    assert not request_file.exists()


def test_parallel_queue_only_requests_are_all_retained(request_file):
    tools = _registered_tools()
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(
                tools["fl_send_notes"], [{"midi": midi, "duration": 1}], auto_trigger=False
            )
            for midi in range(60, 68)
        ]
        for future in futures:
            future.result(timeout=2)
    queued = json.loads(request_file.read_text())
    assert sorted(item["notes"][0]["midi"] for item in queued) == list(range(60, 68))


def test_direct_trigger_waits_for_other_native_operations():
    trigger = object.__new__(fl_trigger.FLStudioTrigger)
    fired = threading.Event()
    entered = threading.Event()
    trigger._trigger_func = lambda: fired.set() or True

    def run():
        entered.set()
        return trigger.trigger(delay=0)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with midi_connection._mailbox_lock:
            future = executor.submit(run)
            assert entered.wait(1)
            assert not fired.wait(0.05)
        assert future.result(timeout=1)
    assert fired.is_set()


def test_script_completion_requires_a_fresh_response_and_consumed_queue(monkeypatch, request_file):
    tools = _registered_tools()
    response = piano_roll._get_response_file()
    response.write_text('{"status": "success", "requests_processed": 1}')
    monkeypatch.setattr(piano_roll, "get_trigger", lambda: SimpleNamespace(is_supported=True))
    monkeypatch.setattr(piano_roll, "trigger_fl_studio", lambda: True)
    monkeypatch.setattr(piano_roll, "_SCRIPT_WAIT_TIMEOUT", 0)

    result = tools["fl_send_notes"]([{"midi": 60, "duration": 1}])
    assert "completion is unverified" in result
    assert not response.exists()
    preserved = request_file.read_text()
    with pytest.raises(RuntimeError, match="queue is preserved"):
        tools["fl_send_notes"]([{"midi": 64, "duration": 1}], auto_trigger=False)
    assert request_file.read_text() == preserved

    # A response alone cannot release the queue until FL has consumed it.
    response.write_text('{"status": "success", "requests_processed": 1}')
    with pytest.raises(RuntimeError, match="queue is preserved"):
        tools["fl_send_notes"]([{"midi": 64, "duration": 1}], auto_trigger=False)
    request_file.write_text("[]")
    tools["fl_send_notes"]([{"midi": 64, "duration": 1}], auto_trigger=False)
    assert json.loads(request_file.read_text())[0]["notes"][0]["midi"] == 64
    assert piano_roll._pending_script is None


def test_late_failed_script_requires_explicit_queue_clear(monkeypatch, request_file):
    tools = _registered_tools()
    monkeypatch.setattr(piano_roll, "get_trigger", lambda: SimpleNamespace(is_supported=True))
    monkeypatch.setattr(piano_roll, "trigger_fl_studio", lambda: True)
    monkeypatch.setattr(piano_roll, "_SCRIPT_WAIT_TIMEOUT", 0)
    tools["fl_send_notes"]([{"midi": 60, "duration": 1}])
    response = piano_roll._get_response_file()
    response.write_text('{"status": "error", "message": "Could not insert notes"}')
    with pytest.raises(RuntimeError, match="previous Piano Roll script failed"):
        tools["fl_send_notes"]([{"midi": 64, "duration": 1}], auto_trigger=False)
    assert "cleared" in tools["fl_clear_request_queue"]()
    assert not request_file.exists()
    assert piano_roll._pending_script is None


def test_piano_roll_windows_trigger_uses_the_shared_foreground_guard(monkeypatch):
    from fl_studio_mcp.utils import windows_ui
    from fl_studio_mcp.utils.fl_trigger import FLStudioTrigger

    events = []

    class Window:
        def __enter__(self):
            events.append("verified FL")
            return self

        def __exit__(self, *args):
            pass

        def press(self, keys):
            events.append(keys)

    monkeypatch.setattr(windows_ui, "FLWindows", Window)
    assert FLStudioTrigger()._trigger_windows() is True
    assert events == ["verified FL", "ctrl+alt+y"]

    class LostWindow(Window):
        def __enter__(self):
            raise RuntimeError("Foreground changed")

    monkeypatch.setattr(windows_ui, "FLWindows", LostWindow)
    assert FLStudioTrigger()._trigger_windows() is False
    assert events == ["verified FL", "ctrl+alt+y"]
