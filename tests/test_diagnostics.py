"""Project summary uses one beat unit for each paired tempo/bar-length field."""

from contextlib import contextmanager

import pytest

from fl_studio_mcp.tools import diagnostics
from fl_studio_mcp.utils import tempo_windows


class Registry:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register


class Connection:
    def __init__(self, summary):
        self.summary = summary
        self.locked = False
        self.calls = []

    @contextmanager
    def transaction(self):
        assert not self.locked
        self.locked = True
        try:
            yield self
        finally:
            self.locked = False

    def send_command(self, action):
        assert self.locked
        self.calls.append(action)
        assert action == "system.getProjectSummary"
        return self.summary


def registered_summary(monkeypatch, summary, reader):
    connection = Connection(summary)
    registry = Registry()
    monkeypatch.setattr(diagnostics, "get_connection", lambda: connection)
    monkeypatch.setattr(tempo_windows, "read_display_tempo", reader)
    diagnostics.register_diagnostic_tools(registry)
    return connection, registry.tools["fl_get_project_summary"]


@pytest.mark.parametrize(
    "displayed,quarter,ratio,quarter_beats,displayed_beats",
    [(140, 70, 2, 3, 6), (140, 140, 1, 4, 4), (140, 280, 0.5, 4, 2)],
)
def test_summary_tempo_and_bar_length_use_matching_beat_units(
    monkeypatch, displayed, quarter, ratio, quarter_beats, displayed_beats
):
    original = {
        "success": True,
        "title": "Meter test",
        "channel_count": 5,
        "tempo": quarter,
        "tempo_unit": "quarter_note_bpm",
        "quarter_beats_per_bar": quarter_beats,
        "beats_per_bar": quarter_beats,
        "ppq": 96,
        "ticks_per_bar": 96 * quarter_beats,
    }
    preserved = dict(original)

    def read(connection):
        assert connection.locked
        return {
            "bpm": displayed,
            "quarter_bpm": quarter,
            "tempo_unit": "displayed_bpm",
            "ui_to_quarter_ratio": ratio,
            "verification": "verified_toolbar",
        }

    connection, summary = registered_summary(monkeypatch, original, read)
    result = summary()
    assert result["success"]
    assert result["bpm"] == result["tempo"] == displayed
    assert result["quarter_bpm"] == quarter
    assert result["beats_per_bar"] == displayed_beats
    assert result["beats_per_bar_unit"] == "displayed_beats"
    assert result["quarter_beats_per_bar"] == quarter_beats
    assert result["tempo_unit"] == "displayed_bpm"
    assert result["tempo_verification"] == "verified_toolbar"
    assert result["channel_count"] == 5 and result["title"] == "Meter test"
    assert result["ticks_per_bar"] == 96 * quarter_beats
    assert result["beats_per_bar"] * 60 / result["tempo"] == pytest.approx(
        result["quarter_beats_per_bar"] * 60 / result["quarter_bpm"]
    )
    assert original == preserved
    assert connection.calls == ["system.getProjectSummary"]
    assert not connection.locked


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("Toolbar covered"),
        OSError("Capture failed"),
        ImportError("Windows OCR bindings missing"),
    ],
)
def test_unreadable_displayed_tempo_never_returns_half_mapped_summary(monkeypatch, error):
    original = {
        "success": True,
        "tempo": 70,
        "tempo_unit": "quarter_note_bpm",
        "beats_per_bar": 3,
        "quarter_beats_per_bar": 3,
    }
    preserved = dict(original)

    def fail(connection):
        assert connection.locked
        raise error

    connection, summary = registered_summary(monkeypatch, original, fail)
    result = summary()
    assert result == {"success": False, "error": str(error)}
    assert not {"tempo", "bpm", "quarter_bpm", "beats_per_bar"}.intersection(result)
    assert original == preserved
    assert connection.calls == ["system.getProjectSummary"]
    assert not connection.locked


def test_failed_native_summary_does_not_attempt_ocr_or_retry(monkeypatch):
    original = {"success": False, "error": "Controller unavailable"}

    def must_not_read(connection):
        pytest.fail("A failed native summary must not trigger OCR")

    connection, summary = registered_summary(monkeypatch, original, must_not_read)
    assert summary() == original
    assert connection.calls == ["system.getProjectSummary"]
    assert not connection.locked
