"""Routing tests execute production dispatch against only a fake FL API boundary."""

import importlib.util
import math
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class Mixer:
    def __init__(self):
        self.routes = {(source, 0): 0.8 for source in range(1, 6)}
        self.calls = []
        self.notifications = 0
        self.fail_once = None
        self.reject_level = False

    def trackCount(self):
        return 7  # Master, inserts 1..5, Current 6.

    def getTrackName(self, index):
        return "Master" if index == 0 else "Insert " + str(index)

    def getRouteSendActive(self, source, destination):
        return (source, destination) in self.routes

    def getRouteToLevel(self, source, destination):
        assert (source, destination) in self.routes, "Disabled send levels are not queryable"
        return self.routes[source, destination]

    def setRouteTo(self, source, destination, enabled, update_ui):
        assert update_ui is False
        self.calls.append(("route", source, destination, enabled))
        if self.fail_once == (source, destination):
            self.fail_once = None
            raise RuntimeError("Host refused one route")
        if enabled:
            self.routes.setdefault((source, destination), 0.8)
        else:
            self.routes.pop((source, destination), None)

    def setRouteToLevel(self, source, destination, level):
        assert (source, destination) in self.routes
        self.calls.append(("level", source, destination, level))
        if not self.reject_level:
            self.routes[source, destination] = level

    def afterRoutingChanged(self):
        self.notifications += 1


@pytest.fixture
def routing(monkeypatch):
    mixer = Mixer()
    monkeypatch.setitem(sys.modules, "mixer", mixer)
    spec = importlib.util.spec_from_file_location(
        "routing_under_test", ROOT / "fl_controller/mcp_routing.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, mixer


def test_dispatch_leaves_other_namespaces_alone(routing):
    module, mixer = routing
    assert module.dispatch("channels.getAll", {}) is None
    assert mixer.calls == []


def test_query_includes_enabled_sidechain_and_never_reads_disabled_levels(routing):
    module, mixer = routing
    mixer.routes[1, 2] = 0
    response = module.dispatch("routing.get", {"source_track": 1, "include_disabled": True})
    assert response["count"] == 5
    assert all(route["destination_track"] != 6 for route in response["routes"])
    routes = {route["destination_track"]: route for route in response["routes"]}
    assert routes[0]["source_name"] == "Insert 1"
    assert routes[2]["sidechain_only"] and routes[2]["enabled"]
    assert routes[2]["level"] == 0
    assert routes[3]["level"] is None and not routes[3]["enabled"]
    assert mixer.calls == []


def test_send_level_is_read_back_and_other_routes_are_preserved(routing):
    module, mixer = routing
    mixer.routes[1, 3] = 0.25
    expected = dict(mixer.routes)
    response = module.dispatch(
        "routing.set",
        {
            "source_track": 1,
            "destination_track": 2,
            "level": 0.6,
        },
    )
    assert response["status"] == "verified"
    assert response["routes"][0]["level"] == 0.6
    assert mixer.routes == {**expected, (1, 2): 0.6}
    assert mixer.notifications == 1


def test_enable_existing_route_preserves_its_level_and_disable_removes_link(routing):
    module, mixer = routing
    mixer.routes[1, 2] = 0.31
    response = module.dispatch("routing.set", {"source_track": 1, "destination_track": 2})
    assert response["success"] and mixer.routes[1, 2] == 0.31
    response = module.dispatch(
        "routing.set",
        {
            "source_track": 1,
            "destination_track": 2,
            "enabled": False,
        },
    )
    assert response["routes"][0]["enabled"] is False
    assert (1, 2) not in mixer.routes
    assert mixer.routes[1, 0] == 0.8


def test_zero_level_keeps_enabled_sidechain_without_muting_master(routing):
    module, mixer = routing
    response = module.dispatch(
        "routing.set",
        {
            "source_track": 1,
            "destination_track": 2,
            "level": 0,
        },
    )
    assert response["routes"][0]["sidechain_only"]
    assert mixer.routes[1, 2] == 0 and mixer.routes[1, 0] == 0.8


def test_exclusive_bus_changes_only_requested_sources(routing):
    module, mixer = routing
    mixer.routes[1, 3] = 0.5
    response = module.dispatch(
        "routing.bus",
        {
            "source_tracks": [1, 2],
            "destination_track": 5,
            "level": 0.7,
            "exclusive": True,
        },
    )
    assert response["success"]
    assert {edge: level for edge, level in mixer.routes.items() if edge[0] in (1, 2)} == {
        (1, 5): 0.7,
        (2, 5): 0.7,
    }
    assert mixer.routes[5, 0] == 0.8 and mixer.routes[3, 0] == 0.8


@pytest.mark.parametrize("source,destination", [(0, 1), (1, 1), (1, 6), (6, 1), (-1, 1), (True, 1)])
def test_invalid_track_targets_have_no_side_effects(routing, source, destination):
    module, mixer = routing
    with pytest.raises(ValueError):
        module.dispatch("routing.set", {"source_track": source, "destination_track": destination})
    assert mixer.calls == []


@pytest.mark.parametrize("level", [math.nan, math.inf, -0.1, 1.1, True, "0.5"])
def test_invalid_levels_have_no_side_effects(routing, level):
    module, mixer = routing
    with pytest.raises(ValueError, match="finite normalized"):
        module.dispatch("routing.set", {"source_track": 1, "destination_track": 2, "level": level})
    assert mixer.calls == []


def test_transitive_feedback_and_zero_level_reverse_routes_are_rejected(routing):
    module, mixer = routing
    mixer.routes.update({(2, 3): 0.4, (3, 4): 0.0, (4, 5): 0.2})
    with pytest.raises(ValueError, match="feedback loop"):
        module.dispatch("routing.set", {"source_track": 5, "destination_track": 2, "level": 0})
    assert mixer.calls == []


def test_entire_bus_is_validated_before_first_mutation(routing):
    module, mixer = routing
    mixer.routes[5, 2] = 0.4
    with pytest.raises(ValueError, match="feedback loop"):
        module.dispatch("routing.bus", {"source_tracks": [1, 2], "destination_track": 5})
    assert mixer.calls == []


def test_failure_mid_batch_restores_original_routes_and_levels(routing):
    module, mixer = routing
    mixer.routes[1, 3] = 0.2
    original = dict(mixer.routes)
    mixer.fail_once = (2, 5)
    response = module.dispatch(
        "routing.bus",
        {
            "source_tracks": [1, 2],
            "destination_track": 5,
            "exclusive": True,
            "level": 0.4,
        },
    )
    assert not response["success"] and response["rollback_complete"]
    assert mixer.routes == original


def test_rejected_send_level_is_not_reported_successful(routing):
    module, mixer = routing
    original = dict(mixer.routes)
    mixer.reject_level = True
    response = module.dispatch(
        "routing.set",
        {
            "source_track": 1,
            "destination_track": 2,
            "level": 0.2,
        },
    )
    assert not response["success"] and response["rollback_complete"]
    assert mixer.routes == original


def test_missing_api_is_reported_before_changing_routes(routing, monkeypatch):
    module, mixer = routing
    monkeypatch.delattr(Mixer, "setRouteToLevel")
    with pytest.raises(RuntimeError, match="API 36"):
        module.dispatch("routing.set", {"source_track": 1, "destination_track": 2})
    assert mixer.calls == []


def test_registered_tools_send_explicit_sidechain_and_exclusive_requests(monkeypatch):
    from fl_studio_mcp.tools import routing as tools

    registered = {}
    calls = []

    class Registry:
        def tool(self):
            return lambda function: registered.setdefault(function.__name__, function)

    class Connection:
        def transaction(self):
            return nullcontext()

        def send_command(self, action, params, timeout):
            calls.append((action, params, timeout))
            return {"success": True, "status": "verified"}

    monkeypatch.setattr(tools, "get_connection", Connection)
    tools.register_routing_tools(Registry())
    assert len(registered) == 5
    registered["fl_sidechain_mixer_track"](1, 2)
    assert calls[-1] == (
        "routing.set",
        {
            "source_track": 1,
            "destination_track": 2,
            "enabled": True,
            "level": 0.0,
            "exclusive": False,
        },
        10.0,
    )
    registered["fl_route_to_bus"]([1, 2], 5, exclusive=True)
    assert calls[-1][1]["exclusive"] is True
    assert calls[-1][1]["source_tracks"] == [1, 2]
    registered["fl_set_mixer_route"](1, 2, enabled=False)
    assert calls[-1][1]["enabled"] is False
    assert "level" not in calls[-1][1]


def test_unexpected_unrelated_change_is_not_claimed_fully_rolled_back(routing, monkeypatch):
    module, mixer = routing
    mixer.routes[1, 3] = 0.7
    original = mixer.setRouteTo

    def corrupt_unrelated_send(source, destination, enabled, update_ui):
        original(source, destination, enabled, update_ui)
        if (source, destination, enabled) == (1, 2, 1):
            mixer.routes[1, 3] = 0.1

    monkeypatch.setattr(mixer, "setRouteTo", corrupt_unrelated_send)
    result = module.dispatch(
        "routing.set",
        {
            "source_track": 1,
            "destination_track": 2,
            "level": 0.4,
        },
    )
    assert not result["success"] and not result["rollback_complete"]
    assert "unrelated send" in result["error"]
    assert result["rollback_errors"]
    assert (1, 2) not in mixer.routes
