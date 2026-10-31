"""Public tempo follows the FL display while time math stays in quarter notes."""

from contextlib import contextmanager

import pytest

from fl_studio_mcp.tools import arrangement


class Connection:
    def __init__(self, result):
        self.result = result
        self.locked = 0
        self.calls = []

    @contextmanager
    def transaction(self):
        self.locked += 1
        try:
            yield self
        finally:
            self.locked -= 1

    def send_command(self, action, params=None, timeout=None):
        assert self.locked
        self.calls.append((action, params))
        return dict(self.result)


def display(bpm, quarter, ratio):
    return {
        "bpm": bpm,
        "quarter_bpm": quarter,
        "ui_to_quarter_ratio": ratio,
        "tempo_unit": "displayed_bpm",
        "verification": "verified_toolbar",
    }


def test_displayed_setter_converts_six_eight_and_verifies_under_the_same_lock(monkeypatch):
    conn = Connection({"success": True, "quarter_bpm": 80, "bpm": 80})
    snapshots = iter((display(140, 70, 2), display(160, 80, 2)))

    def read(connection):
        assert connection is conn and conn.locked
        return next(snapshots)

    monkeypatch.setattr(arrangement, "get_connection", lambda: conn)
    monkeypatch.setattr(arrangement, "_display_tempo", read)
    result = arrangement._set_displayed_tempo(160)
    assert result["success"] and result["bpm"] == 160
    assert result["quarter_bpm"] == 80
    assert conn.calls == [("arrangement.setTempo", {"display_bpm": 160.0, "quarter_bpm": 80.0})]
    assert not conn.locked


def test_timing_maps_display_bpm_without_overwriting_signature_verification(monkeypatch):
    conn = Connection(
        {"success": True, "quarter_bpm": 70, "bpm": 70, "verification": "bar_tick_ratio"}
    )
    monkeypatch.setattr(arrangement, "get_connection", lambda: conn)
    monkeypatch.setattr(arrangement, "_display_tempo", lambda connection: display(140, 70, 2))
    result = arrangement._api("getTiming")
    assert result["bpm"] == 140 and result["quarter_bpm"] == 70
    assert result["verification"] == "bar_tick_ratio"
    assert result["tempo_verification"] == "verified_toolbar"


def test_display_mismatch_is_unknown_without_repeating_the_write(monkeypatch):
    conn = Connection({"success": True, "quarter_bpm": 80})
    snapshots = iter((display(140, 70, 2), display(80, 80, 1)))
    monkeypatch.setattr(arrangement, "get_connection", lambda: conn)
    monkeypatch.setattr(arrangement, "_display_tempo", lambda connection: next(snapshots))
    result = arrangement._set_displayed_tempo(160)
    assert not result["success"] and result["status"] == "unknown"
    assert result["inspect_project_before_retry"]
    assert len(conn.calls) == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, 9, 523])
def test_bad_displayed_bpm_never_reads_ui_or_mutates_the_host(monkeypatch, value):
    monkeypatch.setattr(
        arrangement, "get_connection", lambda: pytest.fail("no connection expected")
    )
    assert arrangement._set_displayed_tempo(value)["status"] == "failed"


def test_failed_ui_preflight_does_not_write_a_quarter_value(monkeypatch):
    conn = Connection({"success": True})
    monkeypatch.setattr(arrangement, "get_connection", lambda: conn)

    def unreadable(connection):
        raise RuntimeError("Toolbar is covered")

    monkeypatch.setattr(arrangement, "_display_tempo", unreadable)
    result = arrangement._set_displayed_tempo(140)
    assert result["status"] == "failed" and not conn.calls


def test_unreadable_display_preflight_does_not_change_time_signature(monkeypatch):
    conn = Connection({"success": True})
    monkeypatch.setattr(arrangement, "get_connection", lambda: conn)

    def unreadable(connection):
        assert connection.locked
        raise RuntimeError("Toolbar is covered")

    monkeypatch.setattr(arrangement, "_display_tempo", unreadable)
    result = arrangement._api("setTimeSignature", {"numerator": 6, "denominator": 8})
    assert not result["success"] and not conn.calls
    assert result["status"] == "failed" and not result["inspect_project_before_retry"]
    assert not conn.locked
