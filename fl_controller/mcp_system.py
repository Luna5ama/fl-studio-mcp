"""Read-only host capabilities and project metadata from the FL MIDI API."""

import math

import channels
import general
import mixer
import ui

PROTOCOL_VERSION = 2


def _tempo_bpm():
    # Some releases invert asInt. The integer representation is millitempo;
    # meter-dependent quarter BPM can legitimately be outside the UI's range.
    values = [mixer.getCurrentTempo(mode) for mode in (False, True)]
    valid = [
        value
        for value in values
        if not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    ]
    if len(valid) == 2 and math.isclose(max(valid), min(valid) * 1000, rel_tol=1e-6):
        return float(min(valid))
    floats = [value for value in valid if isinstance(value, float)]
    if floats:
        return floats[0]
    if valid:
        return float(valid[0])
    raise RuntimeError("FL did not return a finite positive quarter-note BPM value")


def dispatch(action, params):
    if action == "system.getHint":
        return {"hint": ui.getHintMsg()}
    if action == "system.getCapabilities":
        return {
            "protocol_version": PROTOCOL_VERSION,
            "fl_version": ui.getVersion(5),
            "midi_api_version": general.getVersion(),
            "browser_navigation": hasattr(ui, "toggleBrowserNode"),
            "send_levels": hasattr(mixer, "getRouteToLevel"),
            "plugin_mix": hasattr(mixer, "getPluginMixLevel"),
            "tempo_bpm": _tempo_bpm(),
            "tempo_unit": "quarter_note_bpm",
            "project_title": general.getProjectTitle()
            if hasattr(general, "getProjectTitle")
            else None,
            "popup_open": bool(ui.isInPopupMenu()),
            "safe_to_edit": bool(general.safeToEdit()) if hasattr(general, "safeToEdit") else None,
        }
    if action == "system.getProjectSummary":
        return {
            "title": general.getProjectTitle() if hasattr(general, "getProjectTitle") else None,
            "changed": general.getChangedFlag(),
            "channel_count": channels.channelCount(True),
            "mixer_track_count": mixer.trackCount() - 1,
            "selected_mixer_track": mixer.trackNumber(),
            "tempo": _tempo_bpm(),
            "tempo_unit": "quarter_note_bpm",
            "ppq": general.getRecPPQ(),
            "ticks_per_bar": general.getRecPPB(),
            "beats_per_bar": general.getRecPPB() / general.getRecPPQ(),
            "quarter_beats_per_bar": general.getRecPPB() / general.getRecPPQ(),
            "time_signature_numerator": None,
            "time_signature_denominator": None,
        }
    return None
