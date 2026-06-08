from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.providers.config import parse_config_layer


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def _layer(tmp_path: Path, body: str) -> dict:
    layer, _src = parse_config_layer(_write(tmp_path, body))  # type: ignore[misc]
    return layer


_OK_BODY = (
    "providers: [claude_code, openai]\n"
    "tiers:\n"
    "  low: {claude_code: haiku, openai: gpt-5.4-mini}\n"
    "  medium: {claude_code: sonnet, openai: gpt-5.3-codex}\n"
    "  high: {claude_code: opus, openai: gpt-5.5}\n"
)


def test_parse_layer_basic(tmp_path: Path) -> None:
    layer = _layer(tmp_path, _OK_BODY)
    assert layer["providers"] == ("claude_code", "openai")
    assert set(layer["tiers"]) == {"low", "medium", "high"}
    assert layer["tiers"]["medium"]["openai"] == "gpt-5.3-codex"


def test_theme_absent_key_omitted(tmp_path: Path) -> None:
    # A layer that omits `theme:` sets no theme leaf (the floor/default supplies
    # 'ansi' at merge time); parse_config_layer only carries keys the file sets.
    assert "theme" not in _layer(tmp_path, _OK_BODY)


def test_theme_parsed_and_validated(tmp_path: Path) -> None:
    for sub in ("d", "l", "x"):
        (tmp_path / sub).mkdir()
    assert _layer(tmp_path / "d", _OK_BODY + "theme: dark\n")["theme"] == "dark"
    assert _layer(tmp_path / "l", _OK_BODY + "theme: light\n")["theme"] == "light"
    with pytest.raises(LoadError, match="theme"):
        _layer(tmp_path / "x", _OK_BODY + "theme: neon\n")


def test_absent_providers_tolerated(tmp_path: Path) -> None:
    # A layer omitting `providers:` no longer raises — the provider-code floor
    # supplies the provider list at merge time (per-file enforcement removed by
    # the config-split design; was previously load_config raising "providers").
    layer = _layer(tmp_path,
        "tiers:\n"
        "  low: {claude_code: haiku}\n"
        "  medium: {claude_code: sonnet}\n"
        "  high: {claude_code: opus}\n")
    assert "providers" not in layer


def test_partial_tiers_is_valid(tmp_path: Path) -> None:
    # Missing tiers are allowed — the provider-code floor fills them at merge
    # time (spec §7 relaxed _parse_tiers).
    layer = _layer(tmp_path,
        "providers: [claude_code]\n"
        "tiers:\n"
        "  low: {claude_code: haiku}\n"
        "  medium: {claude_code: sonnet}\n")
    assert set(layer["tiers"]) == {"low", "medium"}


def test_absent_file_returns_none(tmp_path: Path) -> None:
    # An absent layer file yields None (the floor/other layers supply config),
    # replacing the old load_config "config.yaml not found" raise.
    assert parse_config_layer(tmp_path / "config.yaml") is None


def test_unknown_tier_rejected(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="tiers:"):
        _layer(tmp_path,
            "providers: [claude_code]\n"
            "tiers:\n"
            "  low: {claude_code: haiku}\n"
            "  medium: {claude_code: sonnet}\n"
            "  high: {claude_code: opus}\n"
            "  extra: {claude_code: opus}\n")


def test_provider_envs_absent_omitted(tmp_path: Path) -> None:
    assert "provider_envs" not in _layer(tmp_path, _OK_BODY)


def test_provider_envs_override(tmp_path: Path) -> None:
    layer = _layer(tmp_path,
        "provider_envs:\n  openai: {env: OAI_KEY}\n" + _OK_BODY)
    assert layer["provider_envs"] == {"openai": "OAI_KEY"}


def test_parses_lint_command(tmp_path: Path) -> None:
    layer = _layer(tmp_path, "lint_command: \"make lint\"\n" + _OK_BODY)
    assert layer["lint_command"] == "make lint"


def test_lint_command_absent_omitted(tmp_path: Path) -> None:
    assert "lint_command" not in _layer(tmp_path, _OK_BODY)


def test_lint_command_not_string_raises(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="lint_command"):
        _layer(tmp_path, "lint_command: [make, lint]\n" + _OK_BODY)


def test_parses_test_command(tmp_path: Path) -> None:
    layer = _layer(tmp_path, "test_command: \"make test\"\n" + _OK_BODY)
    assert layer["test_command"] == "make test"


def test_test_command_absent_omitted(tmp_path: Path) -> None:
    assert "test_command" not in _layer(tmp_path, _OK_BODY)


def test_test_command_not_string_raises(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="test_command"):
        _layer(tmp_path, "test_command: [make, test]\n" + _OK_BODY)


def test_editing_mode_absent_omitted(tmp_path: Path) -> None:
    assert "editing_mode" not in _layer(tmp_path, _OK_BODY)


def test_editing_mode_vi_accepted(tmp_path: Path) -> None:
    assert _layer(tmp_path, "editing_mode: vi\n" + _OK_BODY)["editing_mode"] == "vi"


def test_editing_mode_rejects_unknown(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="editing_mode"):
        _layer(tmp_path, "editing_mode: nano\n" + _OK_BODY)


def test_providers_duplicate_rejected(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="duplicate"):
        _layer(tmp_path,
            "providers: [openai, openai]\n"
            "tiers:\n"
            "  low: {openai: gpt-5.4-mini}\n"
            "  medium: {openai: gpt-5.3-codex}\n"
            "  high: {openai: gpt-5.5}\n")
