"""Verified Mixer sends and sidechains using FL's MIDI scripting API 36+.

Image-Line reference: html/midi_scripting.htm, Mixer module; html/mixer.htm,
Sidechain Sends. An enabled route with level zero is sidechain-only.
"""

from __future__ import annotations

import math

import mixer


def _require_api():
    required = ("getRouteSendActive", "setRouteTo", "getRouteToLevel", "setRouteToLevel")
    if any(not hasattr(mixer, name) for name in required):
        raise RuntimeError("Mixer send/sidechain routing requires FL MIDI scripting API 36+")


def _track(value, name="track", source=False):
    # trackCount includes Master and the final special Current utility track.
    if type(value) is not int or not 0 <= value < mixer.trackCount() - 1:
        raise ValueError(
            name + " must name a real Mixer track; Current is not a routing destination"
        )
    if source and value == 0:
        raise ValueError("Master cannot be the source of an internal Mixer send")
    return value


def _level(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("level must be a finite normalized number from 0 to 1")
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("level must be a finite normalized number from 0 to 1")
    return value


def _route(source, destination):
    enabled = bool(mixer.getRouteSendActive(source, destination))
    level = float(mixer.getRouteToLevel(source, destination)) if enabled else None
    return {
        "source_track": source,
        "destination_track": destination,
        "enabled": enabled,
        "level": level,
        "sidechain_only": enabled and level == 0.0,
    }


def _outgoing(source):
    return {
        destination: _route(source, destination)
        for destination in range(mixer.trackCount() - 1)
        if destination != source
    }


def _same(left, right):
    if left["enabled"] != right["enabled"]:
        return False
    return not left["enabled"] or abs(left["level"] - right["level"]) <= 0.0001


def _would_feedback(source, destination):
    # Include zero-level routes: sidechain signals also participate in FL's graph.
    pending = [destination]
    visited = set()
    while pending:
        node = pending.pop()
        if node == source:
            return True
        if node in visited or node == 0:
            continue
        visited.add(node)
        pending.extend(
            target
            for target in range(mixer.trackCount() - 1)
            if target != node and bool(mixer.getRouteSendActive(node, target))
        )
    return False


def _get(params):
    source = params.get("source_track")
    destination = params.get("destination_track")
    include_disabled = params.get("include_disabled", False)
    if type(include_disabled) is not bool:
        raise ValueError("include_disabled must be a boolean")
    if source is not None:
        source = _track(source, "source_track")
    if destination is not None:
        destination = _track(destination, "destination_track")
    sources = range(1, mixer.trackCount() - 1) if source is None else [source]
    destinations = range(mixer.trackCount() - 1) if destination is None else [destination]
    names = {}
    routes = []
    for track in sources:
        for target in destinations:
            if track == target:
                continue
            route = _route(track, target)
            if include_disabled or route["enabled"]:
                for index in (track, target):
                    if index not in names:
                        names[index] = mixer.getTrackName(index) or (
                            "Master" if index == 0 else "Insert " + str(index)
                        )
                route.update(source_name=names[track], destination_name=names[target])
                routes.append(route)
    return {"success": True, "routes": routes, "count": len(routes)}


def _restore(snapshots, touched):
    errors = []
    for source, destination in reversed(touched):
        original = snapshots[source][destination]
        try:
            mixer.setRouteTo(source, destination, int(original["enabled"]), False)
            if original["enabled"]:
                mixer.setRouteToLevel(source, destination, original["level"])
            if not _same(_route(source, destination), original):
                errors.append("Route was not restored: " + str((source, destination)))
        except Exception as exc:
            errors.append(str(exc))
    try:
        mixer.afterRoutingChanged()
    except Exception as exc:
        errors.append(str(exc))
    for source, original_routes in snapshots.items():
        try:
            current = _outgoing(source)
            if any(
                not _same(current[target], original) for target, original in original_routes.items()
            ):
                errors.append("Source routing was not fully restored: " + str(source))
        except Exception as exc:
            errors.append(str(exc))
    return errors


def _set(params, bus=False):
    destination = _track(params.get("destination_track"), "destination_track")
    enabled = params.get("enabled", True)
    exclusive = params.get("exclusive", False)
    if type(enabled) is not bool or type(exclusive) is not bool:
        raise ValueError("enabled and exclusive must be booleans")
    if exclusive and not enabled:
        raise ValueError("exclusive is only valid when enabling a route")
    level = params.get("level")
    if level is not None:
        level = _level(level)
        if not enabled:
            raise ValueError("Omit level when disabling a route")
    sources = params.get("source_tracks") if bus else [params.get("source_track")]
    if not isinstance(sources, list) or not sources:
        raise ValueError("source_tracks must contain at least one Mixer insert")
    sources = [_track(source, "source_track", source=True) for source in sources]
    if len(sources) != len(set(sources)):
        raise ValueError("source_tracks must not contain duplicates")
    for source in sources:
        if source == destination:
            raise ValueError("A Mixer track cannot route to itself")
        if enabled and _would_feedback(source, destination):
            raise ValueError("The requested route would create a Mixer feedback loop")
    # Validate the entire batch before changing any route. All new bus edges point
    # to the same destination, so reachability above also covers their combination.
    snapshots = {source: _outgoing(source) for source in sources}
    touched = []
    requested = {}
    try:
        for source in sources:
            original = snapshots[source][destination]
            # Use explicit enable/disable values, never a toggle.
            touched.append((source, destination))
            mixer.setRouteTo(source, destination, int(enabled), False)
            actual = _route(source, destination)
            if actual["enabled"] != enabled:
                raise RuntimeError("FL Studio did not accept the requested routing state")
            desired_level = level
            if enabled:
                if desired_level is None:
                    desired_level = original["level"] if original["enabled"] else actual["level"]
                mixer.setRouteToLevel(source, destination, desired_level)
            requested[source] = {"enabled": enabled, "level": desired_level}
            if exclusive:
                for target, route in snapshots[source].items():
                    if target != destination and route["enabled"]:
                        touched.append((source, target))
                        mixer.setRouteTo(source, target, 0, False)
        mixer.afterRoutingChanged()
        results = []
        for source in sources:
            actual = _outgoing(source)
            expected = requested[source]
            if not _same(actual[destination], expected):
                raise RuntimeError("Mixer routing readback differs from the requested state")
            for target, original in snapshots[source].items():
                if target == destination:
                    continue
                expected_route = {"enabled": False, "level": None} if exclusive else original
                if not _same(actual[target], expected_route):
                    raise RuntimeError(
                        "An unrelated send changed or an exclusive route remained active"
                    )
            results.append(actual[destination])
        return {
            "success": True,
            "status": "verified",
            "routes": results,
            "exclusive": exclusive,
        }
    except Exception as exc:
        errors = _restore(snapshots, touched)
        return {
            "success": False,
            "status": "failed",
            "error": str(exc),
            "rollback_complete": not errors,
            "rollback_errors": errors,
            "source_tracks": sources,
            "destination_track": destination,
        }


def dispatch(action: str, params: dict) -> dict | None:
    """Return None for another module's namespace; raise on invalid routing input."""
    if not action.startswith("routing."):
        return None
    _require_api()
    if action == "routing.get":
        return _get(params)
    if action == "routing.set":
        return _set(params)
    if action == "routing.bus":
        return _set(params, bus=True)
    raise ValueError("Unknown routing action: " + action)
