"""File operations are accepted only through independently checked host/disk results."""

import importlib.util
import struct
import sys
import types
from contextlib import nullcontext
from pathlib import Path

import pytest

from fl_studio_mcp.tools import project_files as tools
from fl_studio_mcp.utils import project_files_windows as native

ROOT = Path(__file__).resolve().parents[1]


def wav_bytes(*, rate=48000, channels=2, bits=24, tag=1, frames=40):
    alignment = channels * bits // 8
    fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * alignment, alignment, bits)
    data = b"\0" * (alignment * frames)
    chunks = (
        b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    )
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def flp_bytes():
    data = bytes((0, 1, 2, 3, 4))
    return (
        b"FLhd" + struct.pack("<IHHH", 6, 0, 1, 96) + b"FLdt" + struct.pack("<I", len(data)) + data
    )


def test_wav_inspection_reports_actual_format_and_frames(tmp_path):
    output = tmp_path / "test.wav"
    output.write_bytes(wav_bytes(rate=44100, bits=24, frames=441))
    result = native.inspect_wav(output)
    assert result["sample_rate"] == 44100
    assert result["bits_per_sample"] == 24
    assert result["frames"] == 441
    assert result["duration_seconds"] == pytest.approx(0.01)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data[:-1],
        lambda data: data[:4] + struct.pack("<I", 1) + data[8:],
        lambda data: data[:22] + struct.pack("<I", 0) + data[26:],
        lambda data: data[:32] + struct.pack("<H", 1) + data[34:],
        lambda data: data[:4] + struct.pack("<I", len(data) - 5) + data[8:] + b"bad",
    ],
)
def test_wav_inspection_rejects_partial_or_invalid_audio(tmp_path, mutate):
    output = tmp_path / "test.wav"
    output.write_bytes(mutate(wav_bytes()))
    with pytest.raises(ValueError):
        native.inspect_wav(output)


def test_float_wav_is_not_misreported_as_integer_pcm(tmp_path):
    output = tmp_path / "float.wav"
    output.write_bytes(wav_bytes(tag=3, bits=32))
    assert native.inspect_wav(output)["audio_format"] == "float"


def test_flp_header_and_declared_event_chunk_are_checked(tmp_path):
    output = tmp_path / "test.flp"
    output.write_bytes(flp_bytes())
    assert native.inspect_flp(output)["ppq"] == 96
    output.write_bytes(flp_bytes()[:-1])
    with pytest.raises(ValueError, match="incomplete"):
        native.inspect_flp(output)
    output.write_bytes(b"not a project")
    with pytest.raises(ValueError, match="header"):
        native.inspect_flp(output)


def test_output_validation_refuses_implicit_overwrite_or_relative_paths(tmp_path):
    existing = tmp_path / "existing.flp"
    existing.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        native.output_path(str(existing), ".flp")
    assert native.output_path(str(existing), ".flp", True) == existing
    with pytest.raises(ValueError, match="absolute"):
        native.output_path("relative.flp", ".flp")
    with pytest.raises(ValueError, match="end in"):
        native.output_path(str(tmp_path / "wrong.wav"), ".flp")
    with pytest.raises(ValueError, match="parent"):
        native.output_path(str(tmp_path / "missing" / "test.flp"), ".flp")


def test_audio_input_requires_an_existing_supported_file(tmp_path):
    sample = tmp_path / "音色.wav"
    sample.write_bytes(wav_bytes())
    assert native.input_audio_path(str(sample)) == sample
    with pytest.raises(FileNotFoundError):
        native.input_audio_path(str(tmp_path / "absent.wav"))
    wrong = tmp_path / "plugin.dll"
    wrong.write_bytes(b"no")
    with pytest.raises(ValueError):
        native.input_audio_path(str(wrong))


def test_publishing_wav_refuses_a_destination_race(tmp_path):
    staged = tmp_path / "stage.wav"
    staged.write_bytes(wav_bytes())
    target = tmp_path / "target.wav"
    target.write_bytes(b"previous")
    with pytest.raises(FileExistsError):
        native._publish_file(staged, target, False)
    assert target.read_bytes() == b"previous"
    native._publish_file(staged, target, True)
    assert native.inspect_wav(target)["frames"] == 40


@pytest.fixture
def controller(monkeypatch):
    state = types.SimpleNamespace(
        names=["Existing"], types=[2], selected=0, routes=[0], focused=0, changed=1, calls=[]
    )
    channels = types.SimpleNamespace(
        channelCount=lambda use_global: len(state.names),
        channelNumber=lambda use_global: state.selected,
        getChannelName=lambda index, use_global: state.names[index],
        getChannelType=lambda index, use_global: state.types[index],
        getRecEventId=lambda index, use_global: 1000 + index,
        getTargetFxTrack=lambda index, use_global: state.routes[index],
        setTargetFxTrack=lambda index, route, use_global: state.routes.__setitem__(index, route),
        setChannelName=lambda index, name, use_global: state.names.__setitem__(index, name),
        selectOneChannel=lambda index, use_global: setattr(state, "selected", index),
    )
    for name, module in {
        "channels": channels,
        "general": types.SimpleNamespace(getChangedFlag=lambda: state.changed),
        "midi": types.SimpleNamespace(
            widPlaylist=2,
            widChannelRack=1,
            FPT_Menu=90,
            FPT_ItemMenu=91,
            PME_System=1,
            PME_FromMIDI=32,
        ),
        "mixer": types.SimpleNamespace(trackCount=lambda: 6),
        "transport": types.SimpleNamespace(
            isPlaying=lambda: False,
            stop=lambda: state.calls.append("stop"),
            globalTransport=lambda *args: state.calls.append(args),
        ),
        "ui": types.SimpleNamespace(
            isInPopupMenu=lambda: False,
            getFocused=lambda window: state.focused == window,
            showWindow=lambda window: state.calls.append(("show", window)),
            setFocused=lambda window: setattr(state, "focused", window),
        ),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location(
        "files_controller", ROOT / "fl_controller/mcp_project_files.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, state


def test_file_shortcuts_are_prepared_in_playlist_context(controller):
    module, state = controller
    response = module.dispatch(
        "projectFiles.prepare", {"target": "playlist", "stop_playback": True}
    )
    assert response["target"] == "playlist"
    assert state.focused == 2
    assert state.calls == ["stop", ("show", 2)]
    assert module.dispatch("routing.get", {}) is None


def test_import_requires_exactly_one_new_correct_type_and_sample_label(controller):
    module, state = controller
    assert (
        module.dispatch("projectFiles.inspectNewChannel", {"before_count": 1})["status"]
        == "waiting"
    )
    state.names.append("Sampler")
    state.types.append(0)
    state.routes.append(0)
    state.selected = 1
    original = [{"name": "Existing", "channel_type": 2, "mixer_track": 0, "event_id": 1000}]
    response = module.dispatch(
        "projectFiles.inspectNewChannel",
        {"before_count": 1, "before_channels": original, "channel_type": 0},
    )
    assert response["channel"]["index"] == 1
    args = {
        "index": 1,
        "before_count": 1,
        "channel_type": 0,
        "expected_stem": "tone",
        "mixer_track": 2,
        "channel_name": "Imported",
        "initial_name": "Sampler",
        "before_channels": original,
    }
    assert module.dispatch("projectFiles.finishImport", args)["status"] == "waiting"
    assert state.routes[1] == 0 and state.names[0] == "Existing"
    state.names[1] = "tone"
    response = module.dispatch("projectFiles.finishImport", args)
    assert response["status"] == "imported"
    assert response["channel"] == {
        "index": 1,
        "name": "Imported",
        "channel_type": 0,
        "mixer_track": 2,
    }
    assert state.names[0] == "Existing"


def test_import_rejects_a_changed_destination_before_modifying_it(controller):
    module, state = controller
    state.names.append("tone")
    state.types.append(2)
    state.routes.append(0)
    with pytest.raises(RuntimeError, match="changed type"):
        module.dispatch(
            "projectFiles.finishImport",
            {
                "index": 1,
                "before_count": 1,
                "channel_type": 0,
                "expected_stem": "tone",
                "mixer_track": 2,
                "before_channels": [
                    {"name": "Existing", "channel_type": 2, "mixer_track": 0, "event_id": 1000}
                ],
            },
        )
    assert state.routes == [0, 0]
    with pytest.raises(ValueError, match="Invalid"):
        module.dispatch("projectFiles.focusChannel", {"index": -1, "channel_type": 0})


@pytest.fixture
def registered(monkeypatch):
    functions = {}
    actions = []

    class Registry:
        def tool(self):
            return lambda function: functions.setdefault(function.__name__, function)

    class Connection:
        mode = "pattern"

        def transaction(self):
            return nullcontext(self)

        def send_command(self, action, params=None):
            actions.append((action, params))
            if action == "transport.setLoopMode":
                self.mode = params["mode"]
            return {
                "loop_mode": self.mode,
                "success": True,
                "changed": 0,
                "count": 6,
                "start_ticks": 0,
                "end_ticks": 96,
            }

    monkeypatch.setattr(tools, "get_connection", Connection)
    tools.register_project_file_tools(Registry())
    return functions, actions


def test_save_rejects_overwrite_without_sending_any_host_commands(registered, tmp_path):
    functions, actions = registered
    output = tmp_path / "keep.flp"
    output.write_bytes(b"keep")
    result = functions["fl_save_project"](str(output))
    assert not result["success"] and result["status"] == "rejected"
    assert actions == [] and output.read_bytes() == b"keep"


def test_save_success_requires_disk_result_and_clean_host_readback(
    registered, monkeypatch, tmp_path
):
    functions, actions = registered
    output = tmp_path / "project.flp"
    prepared = []
    monkeypatch.setattr(tools, "prepare_playlist", lambda conn: prepared.append(True))

    def save(path, overwrite, timeout):
        assert prepared
        path.write_bytes(flp_bytes())
        return {"status": "saved", "path": str(path), **native.inspect_flp(path)}

    monkeypatch.setattr(tools, "save_project_file", save)
    result = functions["fl_save_project"](str(output))
    assert result["success"] and result["status"] == "saved"
    assert actions[-1][0] == "projectFiles.status"


def test_render_does_not_claim_completion_after_only_a_keyboard_request(
    registered, monkeypatch, tmp_path
):
    functions, actions = registered
    monkeypatch.setattr(tools, "prepare_playlist", lambda conn, stop_playback: None)

    def incomplete(*args):
        raise TimeoutError("WAV RIFF length does not match the completed file")

    monkeypatch.setattr(tools, "render_wav_file", incomplete)
    result = functions["fl_export_wav"](str(tmp_path / "render.wav"))
    assert not result["success"] and result["status"] == "unknown"
    assert result["inspect_render_before_retry"]


@pytest.mark.parametrize("timeout", [float("inf"), float("nan"), -1, True, 7201])
def test_invalid_export_timeout_is_rejected_before_host_mutation(registered, tmp_path, timeout):
    functions, actions = registered
    result = functions["fl_export_wav"](str(tmp_path / "render.wav"), timeout=timeout)
    assert not result["success"] and result["status"] == "rejected"
    assert actions == []


def test_import_refuses_ambiguous_existing_channel_identity(controller):
    module, state = controller
    state.names[:] = ["Sampler", "Sampler"]
    state.types[:] = [0, 0]
    state.routes[:] = [0, 0]
    state.selected = 0
    original = [{"name": "Sampler", "channel_type": 0, "mixer_track": 0, "event_id": 1000}]
    with pytest.raises(RuntimeError, match="uniquely identify"):
        module.dispatch(
            "projectFiles.inspectNewChannel",
            {"before_count": 1, "before_channels": original, "channel_type": 0},
        )


def test_import_refuses_an_unchanged_empty_channel_label(controller):
    module, state = controller
    state.names.append("Sampler")
    state.types.append(0)
    state.routes.append(0)
    with pytest.raises(ValueError, match="empty channel name"):
        module.dispatch(
            "projectFiles.finishImport",
            {
                "index": 1,
                "before_count": 1,
                "channel_type": 0,
                "expected_stem": "Sampler",
                "initial_name": "Sampler",
                "before_channels": [
                    {"name": "Existing", "channel_type": 2, "mixer_track": 0, "event_id": 1000}
                ],
            },
        )


def test_import_ambiguous_source_name_is_rejected_before_host_mutation(registered, tmp_path):
    functions, actions = registered
    sample = tmp_path / "Sampler.wav"
    sample.write_bytes(wav_bytes())
    result = functions["fl_import_sample"](str(sample))
    assert not result["success"] and result["status"] == "rejected"
    assert actions == []


def test_export_restores_original_mode_after_render_failure(registered, monkeypatch, tmp_path):
    functions, actions = registered
    monkeypatch.setattr(tools, "prepare_playlist", lambda conn, stop_playback: None)

    def failed(*args):
        raise TimeoutError("Render still active")

    monkeypatch.setattr(tools, "render_wav_file", failed)
    result = functions["fl_export_wav"](str(tmp_path / "render.wav"))
    assert result["status"] == "unknown"
    assert result["transport_mode_restoration"] == {
        "attempted": True,
        "mode": "pattern",
        "actual_mode": "pattern",
        "success": True,
    }
    assert [params["mode"] for action, params in actions if action == "transport.setLoopMode"] == [
        "song",
        "pattern",
    ]
    assert (
        result["selection_before"]
        == result["selection_after"]
        == {"start_ticks": 0, "end_ticks": 96}
    )


def test_export_returns_actual_header_values_and_restore_state(registered, monkeypatch, tmp_path):
    functions, actions = registered
    monkeypatch.setattr(tools, "prepare_playlist", lambda conn, stop_playback: None)

    def render(conn, path, overwrite, timeout, scope):
        path.write_bytes(wav_bytes(rate=96000, bits=32, tag=3, frames=192))
        return {"status": "rendered", "path": str(path), **native.inspect_wav(path)}

    monkeypatch.setattr(tools, "render_wav_file", render)
    result = functions["fl_export_wav"](
        str(tmp_path / "render.wav"), render_scope="current_selection"
    )
    assert result["success"] and result["sample_rate"] == 96000
    assert result["bits_per_sample"] == 32 and result["audio_format"] == "float"
    assert result["frames"] == 192 and result["channels"] == 2
    assert result["transport_mode_restoration"]["success"]


def test_export_reports_failed_mode_restoration_with_verified_audio(monkeypatch, tmp_path):
    functions = {}

    class Registry:
        def tool(self):
            return lambda function: functions.setdefault(function.__name__, function)

    class Connection:
        mode = "pattern"

        def transaction(self):
            return nullcontext(self)

        def send_command(self, action, params=None):
            if action == "transport.setLoopMode":
                if params["mode"] == "pattern":
                    raise TimeoutError("Controller unavailable during restoration")
                self.mode = params["mode"]
            return {"success": True, "loop_mode": self.mode, "start_ticks": -1, "end_ticks": -1}

    monkeypatch.setattr(tools, "get_connection", Connection)
    monkeypatch.setattr(tools, "prepare_playlist", lambda conn, stop_playback: None)
    monkeypatch.setattr(tools, "render_wav_file", lambda *args: {"status": "rendered"})
    tools.register_project_file_tools(Registry())
    result = functions["fl_export_wav"](str(tmp_path / "render.wav"))
    assert not result["success"] and result["status"] == "rendered_with_restore_error"
    assert result["verified_audio"] and not result["transport_mode_restoration"]["success"]


def test_render_stages_extra_formats_away_from_user_siblings(monkeypatch, tmp_path):
    target = tmp_path / "output.wav"
    sibling = tmp_path / "output.mp3"
    sibling.write_bytes(b"preserve")

    class Windows:
        root = 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def windows(self):
            return [1]

        def press(self, keys):
            pass

    def submit(windows, dialog, path, button):
        assert path.parent != target.parent
        path.write_bytes(wav_bytes())
        path.with_suffix(".mp3").write_bytes(b"extra format")

    monkeypatch.setattr(native, "FLWindows", Windows)
    monkeypatch.setattr(native, "_file_dialog", lambda *args: 2)
    monkeypatch.setattr(native, "_submit_filename", submit)
    monkeypatch.setattr(native, "_render_window", lambda *args: 3)
    monkeypatch.setattr(native, "_set_render_scope", lambda *args: None)
    monkeypatch.setattr(native, "_click_label", lambda *args: None)
    monkeypatch.setattr(native, "_render_complete", lambda *args: True)
    result = native.render_wav_file(None, target, False, 5, "pattern")
    assert result["frames"] == 40
    assert sibling.read_bytes() == b"preserve"
    assert (Path(result["staging_directory"]) / "render.mp3").read_bytes() == b"extra format"
    assert not (Path(result["staging_directory"]) / "render.wav").exists()


def test_same_name_empty_sampler_is_distinguished_by_event_identity(controller):
    module, state = controller
    state.names[:] = ["Sampler", "Sampler"]
    state.types[:] = [0, 0]
    state.routes[:] = [0, 0]
    state.selected = 1
    original = [{"name": "Sampler", "channel_type": 0, "mixer_track": 0, "event_id": 1000}]
    result = module.dispatch(
        "projectFiles.inspectNewChannel",
        {"before_count": 1, "before_channels": original, "channel_type": 0},
    )
    assert result["status"] == "created" and result["channel"]["index"] == 1


def test_file_prepare_reports_modal_without_issuing_unsafe_ui_call(controller, monkeypatch):
    module, state = controller
    monkeypatch.setattr(module.general, "safeToEdit", lambda: False, raising=False)
    with pytest.raises(RuntimeError, match="modal dialogs"):
        module.dispatch("projectFiles.prepare", {"target": "rack"})
    assert state.calls == []


def test_playlist_preparation_activates_before_controller_focus(monkeypatch):
    active = []

    class Windows:
        def __enter__(self):
            active.append(True)
            return self

        def __exit__(self, *args):
            active.pop()

    class Connection:
        def send_command(self, action, params=None):
            assert active, "Native FL activation must precede focus/menu MIDI actions"
            return {"success": True, "playlist_focused": True}

    monkeypatch.setattr(native, "FLWindows", Windows)
    assert native.prepare_playlist(Connection())["playlist_focused"]
    assert active == []


def test_import_activates_before_preparing_rack(monkeypatch, tmp_path):
    active = []

    class Windows:
        def __enter__(self):
            active.append(True)
            return self

        def __exit__(self, *args):
            active.pop()

    class Connection:
        def send_command(self, action, params=None):
            assert active, "Native FL activation must precede Channel Rack preparation"
            assert action == "projectFiles.prepare"
            return {"success": False, "error": "Stop after the verified preparation boundary"}

    monkeypatch.setattr(native, "FLWindows", Windows)
    result = native.create_sample_channel(
        Connection(), tmp_path / "tone.wav", "sampler", None, None, 5
    )
    assert not result["success"]
    assert active == []


def test_localized_standard_file_dialog_is_identified_by_controls(monkeypatch):
    class Windows:
        root = 1

        def require_foreground(self):
            pass

        def windows(self):
            return [1, 2]

        def class_name(self, hwnd):
            return "#32770" if hwnd == 2 else "TFruityLoopsMainForm"

        def text(self, hwnd):
            return "另存为"

    windows = Windows()
    monkeypatch.setattr(native, "_filename_edit", lambda window, hwnd: 3)
    monkeypatch.setattr(native, "_dialog_button", lambda window, hwnd, control_id: 4)
    assert native._file_dialog(windows, {1}, native.time.monotonic() + 1) == 2


def test_localized_file_submit_uses_idok_not_english_caption(monkeypatch, tmp_path):
    path = tmp_path / "test.flp"
    state = {}

    class Windows:
        def class_name(self, hwnd):
            return "#32770" if hwnd == 2 else "Button"

        def set_text(self, hwnd, text):
            state["text"] = text

        def text(self, hwnd):
            return state["text"] if hwnd == 3 else "保存(&S)"

        def rect(self, hwnd):
            assert hwnd == 4
            return (10, 20, 30, 40)

        def click(self, point):
            state["clicked"] = point

    monkeypatch.setattr(native, "_filename_edit", lambda window, hwnd: 3)
    monkeypatch.setattr(native, "_dialog_button", lambda window, hwnd, control_id: 4)
    native._submit_filename(Windows(), 2, path, "Save")
    assert state == {"text": str(path), "clicked": (20, 30)}


@pytest.mark.parametrize(
    "message,excluded,yes_id,expected",
    [
        ("test.flp 已经存在。是否替换?", set(), 6, True),
        ("othertest.flp 已经存在。是否替换?", set(), 6, False),
        ("test.flp.bak 已经存在。是否替换?", set(), 6, False),
        ("test.flp 已经存在。是否替换?", {2}, 6, False),
        ("test.flp 已经存在。是否替换?", set(), 1, False),
    ],
)
def test_overwrite_confirmation_requires_new_exact_filename_and_idyes(
    monkeypatch, tmp_path, message, excluded, yes_id, expected
):
    from fl_studio_mcp.utils import arrangement_windows

    monkeypatch.setattr(arrangement_windows, "_scan", lambda *args: [])
    clicked = []

    class Windows:
        def windows(self, class_name=None):
            return [2]

        def children(self, hwnd):
            return [3]

        def class_name(self, hwnd):
            return "Static"

        def text(self, hwnd):
            return message

        def rect(self, hwnd):
            return (10, 20, 30, 40)

        def _check_point(self, hwnd, point):
            assert hwnd == 2

        def click(self, point):
            clicked.append(point)

    monkeypatch.setattr(
        native,
        "_dialog_button",
        lambda windows, dialog, control_id: (
            4 if control_id == yes_id else 5 if control_id == 7 else None
        ),
    )
    assert native._confirm_overwrite(Windows(), tmp_path / "test.flp", excluded) is expected
    assert bool(clicked) is expected


def test_sample_tree_menu_prefers_frontmost_exact_arrow_label(monkeypatch):
    clicked = []

    class Windows:
        def require_foreground(self):
            pass

        def windows(self, class_name=None):
            return [3, 2, 1]

        def find_text(self, hwnd, label):
            if hwnd == 3 and label == "Sampler >":
                return (20, 30)
            if hwnd == 2 and label == "Sampler":
                return (10, 30)
            raise RuntimeError("No exact label")

        def _check_point(self, hwnd, point):
            assert hwnd == 3

        def click(self, point):
            clicked.append(point)

    assert native._sample_menu_action(Windows(), "Sampler")
    assert clicked == [(20, 30)]


def test_sample_channel_context_uses_original_unique_label_without_renaming(monkeypatch):
    from fl_studio_mcp.utils import arrangement_windows

    actions = []

    def scan(windows, hwnd, region):
        assert hwnd == 2 and region == (200, 130, 388, 250)
        return [{"text": "Sampler #2", "rect": (200, 150, 280, 170)}]

    monkeypatch.setattr(arrangement_windows, "_scan", scan)
    monkeypatch.setattr(native, "_wait_focus", lambda *args: None)

    class Connection:
        def send_command(self, action, params):
            actions.append(action)
            return {"success": True, "channel": {"name": "Sampler #2"}}

    class Windows:
        def children(self, parent=None, class_name=None, title=None):
            return [3] if class_name == "TNumBar" else [2]

        def rect(self, hwnd):
            return (400, 260, 680, 280) if hwnd == 3 else (100, 100, 700, 280)

        def _check_point(self, hwnd, point):
            assert hwnd == 2

        def click(self, point, button):
            assert point == (240, 160) and button == "right"

    native._open_sample_channel_menu(
        Connection(), Windows(), 1, [{"name": "Sampler"}], 0, "Sampler #2"
    )
    assert actions == ["projectFiles.focusChannel"]


def test_directui_confirmation_button_uses_uia_command_id_and_process(monkeypatch):
    class Windows:
        process_id = 123

        def children(self, dialog):
            return [3, 4]

        def class_name(self, hwnd):
            return "Button"

    monkeypatch.setattr(native, "_control_id", lambda windows, hwnd: 0)
    calls = []

    def automation_id(hwnd, expected_pid):
        calls.append((hwnd, expected_pid))
        return "CommandButton_" + str({3: 6, 4: 7}[hwnd])

    monkeypatch.setattr(native, "_uia_automation_id", automation_id)
    assert native._dialog_button(Windows(), 2, 6) == 3
    assert all(pid == 123 for hwnd, pid in calls)


def test_render_mode_reads_inline_label_and_value(monkeypatch):
    from fl_studio_mcp.utils import arrangement_windows

    class Windows:
        def children(self, dialog, class_name=None, title=None):
            assert class_name == "TVectorPanel" and title == "Project type"
            return [2]

    monkeypatch.setattr(
        arrangement_windows,
        "_scan",
        lambda *args: [
            {
                "text": "Mode Pattern",
                "words": [
                    {"text": "Mode", "rect": (10, 10, 40, 25)},
                    {"text": "Pattern", "rect": (50, 10, 90, 25)},
                ],
            }
        ],
    )
    assert native._render_mode(Windows(), 1) == ("Pattern", (70, 17.5))


def test_already_correct_render_mode_does_not_toggle_it(monkeypatch):
    monkeypatch.setattr(native, "_render_mode", lambda *args: ("Pattern", (10, 20)))
    native._set_render_scope(object(), 1, "pattern")


@pytest.mark.parametrize(
    "label,valid",
    [
        ("I-oad sample„", True),
        ("Load sample...", True),
        ("Delete sample", False),
        ("Replace sample", False),
    ],
)
def test_load_sample_ocr_normalization_is_action_specific(monkeypatch, label, valid):
    from fl_studio_mcp.utils import arrangement_windows

    clicked = []

    class Windows:
        def windows(self, class_name=None):
            return [2]

        def _check_point(self, hwnd, point):
            assert hwnd == 2

        def click(self, point):
            clicked.append(point)

    monkeypatch.setattr(
        arrangement_windows, "_scan", lambda *args: [{"text": label, "rect": (10, 20, 30, 40)}]
    )
    if valid:
        native._choose_load_sample(Windows(), timeout=0.001)
    else:
        monkeypatch.setattr(native.time, "sleep", lambda delay: None)
        with pytest.raises(RuntimeError, match="Load sample"):
            native._choose_load_sample(Windows(), timeout=0.001)
    assert bool(clicked) is valid


def test_transport_mode_readback_waits_without_reissuing_toggle(monkeypatch):
    calls = []
    modes = iter(["song", "song", "pattern"])

    class Connection:
        def send_command(self, action):
            calls.append(action)
            return {"success": True, "loop_mode": next(modes)}

    monkeypatch.setattr(tools.time, "sleep", lambda delay: None)
    assert tools._wait_transport_mode(Connection(), "pattern")["loop_mode"] == "pattern"
    assert calls == ["transport.getStatus"] * 3


def test_selection_render_mode_requires_song_selection_not_full_song(monkeypatch):
    monkeypatch.setattr(native, "_render_mode", lambda *args: ("Song selection", (10, 20)))
    assert native._set_render_scope(object(), 1, "current_selection") == "Song selection"


def test_render_mode_reads_song_selection_from_project_type_panel(monkeypatch):
    from fl_studio_mcp.utils import arrangement_windows

    class Windows:
        def children(self, dialog, class_name=None, title=None):
            return [2]

    monkeypatch.setattr(
        arrangement_windows,
        "_scan",
        lambda *args: [
            {
                "text": "Mode Song selection",
                "words": [
                    {"text": "Mode", "rect": (10, 10, 40, 25)},
                    {"text": "Song", "rect": (50, 10, 75, 25)},
                    {"text": "selection", "rect": (80, 10, 120, 25)},
                ],
            }
        ],
    )
    assert native._render_mode(Windows(), 1) == ("Song selection", (85, 17.5))
