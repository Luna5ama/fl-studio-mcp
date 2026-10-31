"""Regression checks for asynchronous status and native-operation ownership."""

from fl_studio_mcp.utils.runtime_operations import LoadOperations


def test_cancel_is_a_request_not_a_completion():
    registry = LoadOperations()
    registry.begin("one", runner_active=True)
    result = registry.cancel("one")
    assert result["status"] == "cancellation_requested"
    assert registry.get("one")["status"] == "loading"
    assert registry.cancelled("one")
    registry.update({"operation_id": "one", "status": "loaded", "runner_active": False})
    assert registry.cancel("one")["cancellation_requested"] is False


def test_finished_history_is_bounded_and_query_results_are_detached():
    registry = LoadOperations(limit=3)
    for key in ("a", "b", "c", "d"):
        registry.begin(key)
        registry.update({"operation_id": key, "status": "loaded"})
    assert [item["operation_id"] for item in registry.list(3)] == ["d", "c", "b"]
    snapshot = registry.get("d")
    snapshot["status"] = "corrupted"
    assert registry.get("d")["status"] == "loaded"


def test_lost_connection_does_not_leave_a_zombie_loading_record(monkeypatch):
    from fl_studio_mcp.tools import plugin_loading

    registry = LoadOperations()
    monkeypatch.setattr(plugin_loading, "load_operations", registry)

    class Connection:
        def send_command(self, action, params):
            if action == "plugins.beginLoad":
                return {"success": True, "operation_id": "one"}
            raise RuntimeError("MIDI connection lost")

    result = plugin_loading._execute_load(
        Connection(), {"id": "fixture", "name": "Synth"}, "generator", None, 5
    )
    assert result["status"] == "unknown"
    assert registry.get("one")["status"] == "unknown"
    assert registry.get("one")["runner_active"] is False


def test_cancel_during_ocr_blocks_native_confirmation(monkeypatch):
    from fl_studio_mcp.tools import plugin_loading

    registry = LoadOperations()
    actions = []
    monkeypatch.setattr(plugin_loading, "load_operations", registry)

    class Connection:
        def send_command(self, action, params):
            actions.append(action)
            if action == "plugins.beginLoad":
                return {"success": True, "operation_id": "one"}
            if action == "plugins.cancelLoad":
                return {"success": True, "status": "cancelled"}
            return {
                "success": True,
                "operation_id": "one",
                "status": "loading",
                "stage": "awaiting_menu",
                "menu_action": "open in new channel",
            }

    def recognise_then_cancel(action, confirm):
        registry.cancel("one")
        confirm()
        raise AssertionError("Activation must not happen after the cancellation request")

    monkeypatch.setattr(plugin_loading, "select_plugin_menu_action", recognise_then_cancel)
    result = plugin_loading._execute_load(
        Connection(), {"id": "fixture", "name": "Synth"}, "generator", None, 5
    )
    assert result["status"] == "cancelled"
    assert "plugins.confirmLoadMenu" not in actions
    assert registry.get("one")["runner_active"] is False


def test_diagnostics_rejects_legacy_success_error_response(monkeypatch):
    from fl_studio_mcp.tools import diagnostics

    functions = {}

    class Registry:
        def tool(self):
            return lambda function: functions.setdefault(function.__name__, function)

    class Connection:
        def get_status(self):
            return {"connected": True}

        def send_command(self, *args):
            return {"success": True, "error": "Unknown action: system.getCapabilities"}

    monkeypatch.setattr(diagnostics, "get_connection", Connection)
    diagnostics.register_diagnostic_tools(Registry())
    assert functions["fl_check_environment"]()["communication_verified"] is False
