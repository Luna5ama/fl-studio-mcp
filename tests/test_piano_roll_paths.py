"""Keep client and embedded Piano Roll mailboxes together after data relocation."""

import sys
from pathlib import Path
from types import SimpleNamespace

from fl_studio_mcp.tools import piano_roll

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ComposeWithLLM.pyscript"


def _embedded(monkeypatch, baked=None):
    monkeypatch.setitem(sys.modules, "flpianoroll", SimpleNamespace())
    source = SCRIPT.read_text(encoding="utf-8")
    if baked is not None:
        assert source.count("USER_DATA_DIR = None") == 1
        source = source.replace("USER_DATA_DIR = None", f"USER_DATA_DIR = {str(baked)!r}", 1)
    namespace = {"__name__": "piano_roll_path_fixture"}
    exec(compile(source, str(SCRIPT), "exec"), namespace)
    return namespace


def _assert_mailboxes(embedded, directory):
    assert Path(embedded["get_script_dir"]()) == directory
    assert Path(embedded["REQUEST_FILE"]) == piano_roll._get_request_file()
    assert Path(embedded["RESPONSE_FILE"]) == piano_roll._get_response_file()
    assert Path(embedded["STATE_FILE"]) == piano_roll._get_state_file()


def test_environment_relocation_uses_one_chinese_mailbox_directory(monkeypatch, tmp_path):
    user_data = tmp_path / "自定义 用户目录" / "FL Studio"
    monkeypatch.setenv("FL_STUDIO_USER_DATA_DIR", str(user_data))
    directory = user_data / "Settings" / "Piano roll scripts"
    _assert_mailboxes(_embedded(monkeypatch), directory)
    assert directory.is_dir()


def test_installed_baked_directory_takes_precedence_over_environment(monkeypatch, tmp_path):
    baked = tmp_path / "安装指定目录" / "FL Studio"
    monkeypatch.setenv("FL_STUDIO_USER_DATA_DIR", str(tmp_path / "另一个目录"))
    embedded = _embedded(monkeypatch, baked)
    directory = baked / "Settings" / "Piano roll scripts"
    assert Path(embedded["get_script_dir"]()) == directory
    monkeypatch.setenv("FL_STUDIO_USER_DATA_DIR", str(baked))
    _assert_mailboxes(embedded, directory)


def test_tilde_override_expands_consistently_on_both_sides(monkeypatch, tmp_path):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FL_STUDIO_USER_DATA_DIR", "~/音乐制作/FL Studio")
    directory = tmp_path / "音乐制作" / "FL Studio" / "Settings" / "Piano roll scripts"
    _assert_mailboxes(_embedded(monkeypatch), directory)


def test_uninstalled_embedded_script_keeps_legacy_documents_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("FL_STUDIO_USER_DATA_DIR", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    user_data = tmp_path / "Documents" / "Image-Line" / "FL Studio"
    # The installed script gets registry relocation baked by the installer;
    # this case deliberately verifies the uninstalled fallback without registry I/O.
    monkeypatch.setattr(piano_roll, "get_user_data_dir", lambda: user_data)
    directory = user_data / "Settings" / "Piano roll scripts"
    _assert_mailboxes(_embedded(monkeypatch), directory)
