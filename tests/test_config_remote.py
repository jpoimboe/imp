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


def test_parse_remote_bare_string_normalizes_to_name(tmp_path: Path) -> None:
    layer = parse_config_layer(_write(tmp_path, "remote:\n  driver: telegram\n"))
    assert layer is not None
    out, _src = layer
    assert out["remote"] == {"driver": {"name": "telegram"}}


def test_parse_remote_dict_form_keeps_knob_overrides(tmp_path: Path) -> None:
    layer = parse_config_layer(_write(tmp_path, (
        "remote:\n"
        "  driver:\n"
        "    name: telegram\n"
        "    events: [awaiting_user_input, errored]\n"
        "    startup_timeout: 3\n"
        "    reply_timeout: 30\n"
    )))
    assert layer is not None
    out, _src = layer
    assert out["remote"]["driver"] == {
        "name": "telegram",
        "events": ["awaiting_user_input", "errored"],
        "startup_timeout": 3.0,
        "reply_timeout": 30.0,
    }


def test_parse_remote_dict_form_keeps_only_present_knobs(tmp_path: Path) -> None:
    # A partial override carries only the leaves present; unset knobs are absent
    # (the driver falls back to its own defaults), and `reply_timeout: null`
    # round-trips to None — the "wait forever, like a local prompt" form.
    layer = parse_config_layer(_write(tmp_path, (
        "remote:\n"
        "  driver:\n"
        "    name: telegram\n"
        "    reply_timeout: null\n"
    )))
    assert layer is not None
    out, _src = layer
    assert out["remote"]["driver"] == {"name": "telegram", "reply_timeout": None}


def test_parse_remote_rejects_command_key(tmp_path: Path) -> None:
    # `command` is no longer a known driver key (the raw-argv form is removed).
    with pytest.raises(LoadError):
        parse_config_layer(_write(tmp_path,
            "remote:\n  driver:\n    name: telegram\n    command: [x]\n"))


def test_parse_remote_rejects_unknown_name_string(tmp_path: Path) -> None:
    with pytest.raises(LoadError) as exc:
        parse_config_layer(_write(tmp_path, "remote:\n  driver: bogus\n"))
    assert "telegram" in str(exc.value)        # error names the available built-ins


def test_parse_remote_rejects_unknown_name_dict(tmp_path: Path) -> None:
    with pytest.raises(LoadError):
        parse_config_layer(_write(tmp_path,
            "remote:\n  driver:\n    name: bogus\n"))


def test_parse_remote_dict_requires_name(tmp_path: Path) -> None:
    with pytest.raises(LoadError):
        parse_config_layer(_write(tmp_path,
            "remote:\n  driver:\n    events: [all]\n"))


def test_parse_remote_rejects_unknown_remote_key(tmp_path: Path) -> None:
    with pytest.raises(LoadError):
        parse_config_layer(_write(tmp_path, "remote:\n  bogus: 1\n"))


def test_effective_config_surfaces_remote_from_local_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))  # no user config
    (tmp_path / ".imp").mkdir()
    (tmp_path / ".imp" / "config.yaml").write_text("remote:\n  driver: telegram\n")
    cfg = load_effective_config(control_root=tmp_path).config
    assert cfg.remote == {"driver": {"name": "telegram"}}


def test_effective_config_remote_empty_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))  # no user config
    cfg = load_effective_config(control_root=tmp_path).config
    assert cfg.remote == {}
