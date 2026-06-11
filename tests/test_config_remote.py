# tests/test_config_remote.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.providers.config import parse_config_layer
from imp.providers.effective import load_effective_config


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def test_parse_remote_normalizes_driver_with_defaults(tmp_path: Path) -> None:
    layer = parse_config_layer(_write(tmp_path, (
        "remote:\n"
        "  driver:\n"
        "    command: [imp-telegram]\n"
    )))
    assert layer is not None
    out, _src = layer
    # events/startup_timeout/reply_timeout default when omitted (spec §A config).
    assert out["remote"] == {
        "driver": {
            "command": ["imp-telegram"],
            "events": ["all"],
            "startup_timeout": 10.0,
            "reply_timeout": None,
        }
    }


def test_parse_remote_keeps_explicit_values(tmp_path: Path) -> None:
    layer = parse_config_layer(_write(tmp_path, (
        "remote:\n"
        "  driver:\n"
        "    command: [python, drv.py]\n"
        "    events: [awaiting_user_input, errored]\n"
        "    startup_timeout: 3\n"
        "    reply_timeout: 30\n"
    )))
    assert layer is not None
    out, _src = layer
    assert out["remote"]["driver"] == {
        "command": ["python", "drv.py"],
        "events": ["awaiting_user_input", "errored"],
        "startup_timeout": 3.0,
        "reply_timeout": 30.0,
    }


def test_parse_remote_rejects_empty_command(tmp_path: Path) -> None:
    with pytest.raises(LoadError):
        parse_config_layer(_write(tmp_path, "remote:\n  driver:\n    command: []\n"))


def test_parse_remote_rejects_unknown_key(tmp_path: Path) -> None:
    with pytest.raises(LoadError):
        parse_config_layer(_write(tmp_path, "remote:\n  bogus: 1\n"))


def test_parse_remote_rejects_unknown_driver_key(tmp_path: Path) -> None:
    with pytest.raises(LoadError):
        parse_config_layer(_write(tmp_path,
            "remote:\n  driver:\n    command: [foo]\n    starup_timeout: 5\n"))


def test_effective_config_surfaces_remote_from_local_layer(tmp_path: Path) -> None:
    (tmp_path / ".imp").mkdir()
    (tmp_path / ".imp" / "config.yaml").write_text(
        "remote:\n  driver:\n    command: [imp-telegram]\n")
    cfg = load_effective_config(control_root=tmp_path).config
    assert cfg.remote == {
        "driver": {
            "command": ["imp-telegram"],
            "events": ["all"],
            "startup_timeout": 10.0,
            "reply_timeout": None,
        }
    }


def test_effective_config_remote_empty_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))  # no user config
    cfg = load_effective_config(control_root=tmp_path).config
    assert cfg.remote == {}
