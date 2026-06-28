from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.providers.config import Config, parse_config_layer


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


def test_parses_verify_command(tmp_path: Path) -> None:
    layer = _layer(tmp_path, "verify_command: \"make check\"\n" + _OK_BODY)
    assert layer["verify_command"] == "make check"


def test_verify_command_empty_string_preserved(tmp_path: Path) -> None:
    # Tri-state opt-out: an explicit empty string parses to "" (NOT dropped),
    # distinct from key-absent — the gate no-ops but never nudges again.
    layer = _layer(tmp_path, "verify_command: \"\"\n" + _OK_BODY)
    assert layer["verify_command"] == ""


def test_verify_command_absent_omitted(tmp_path: Path) -> None:
    assert "verify_command" not in _layer(tmp_path, _OK_BODY)


def test_verify_command_not_string_raises(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="verify_command"):
        _layer(tmp_path, "verify_command: [make, check]\n" + _OK_BODY)


def test_parses_verify_timeout(tmp_path: Path) -> None:
    layer = _layer(tmp_path, "verify_timeout: 1200\n" + _OK_BODY)
    assert layer["verify_timeout"] == 1200


def test_verify_timeout_absent_omitted(tmp_path: Path) -> None:
    assert "verify_timeout" not in _layer(tmp_path, _OK_BODY)


@pytest.mark.parametrize("bad", ["0", "-5", "\"1200\"", "1.5", "true"])
def test_verify_timeout_invalid_raises(tmp_path: Path, bad: str) -> None:
    # Zero/negative, strings, floats, and bool (an int subclass) are all rejected —
    # only a positive integer number of seconds is a meaningful gate ceiling.
    with pytest.raises(LoadError, match="verify_timeout"):
        _layer(tmp_path, f"verify_timeout: {bad}\n" + _OK_BODY)


def test_editing_mode_absent_omitted(tmp_path: Path) -> None:
    assert "editing_mode" not in _layer(tmp_path, _OK_BODY)


def test_editing_mode_vi_accepted(tmp_path: Path) -> None:
    assert _layer(tmp_path, "editing_mode: vi\n" + _OK_BODY)["editing_mode"] == "vi"


def test_editing_mode_rejects_unknown(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="editing_mode"):
        _layer(tmp_path, "editing_mode: nano\n" + _OK_BODY)


def test_env_for_synthesizes_default_when_unset() -> None:
    """env_for falls back to {VENDOR}_API_KEY when provider_envs has no override."""
    cfg = Config()
    assert cfg.env_for("anthropic") == "ANTHROPIC_API_KEY"
    assert cfg.env_for("openai") == "OPENAI_API_KEY"


def test_env_for_uses_explicit_override() -> None:
    """An explicit provider_envs entry wins over the synthesized default."""
    cfg = Config(provider_envs={"anthropic": "MY_CUSTOM_KEY"})
    assert cfg.env_for("anthropic") == "MY_CUSTOM_KEY"


def test_providers_duplicate_rejected(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="duplicate"):
        _layer(tmp_path,
            "providers: [openai, openai]\n"
            "tiers:\n"
            "  low: {openai: gpt-5.4-mini}\n"
            "  medium: {openai: gpt-5.3-codex}\n"
            "  high: {openai: gpt-5.5}\n")


def test_context_accessors_read_overrides() -> None:
    cfg = Config(context={"checkpoint_threshold": 0.65, "max_reincarnations": 5})
    assert cfg.context_checkpoint_threshold() == 0.65
    assert cfg.context_max_reincarnations() == 5


def test_context_accessors_default_when_unset() -> None:
    cfg = Config()
    assert cfg.context_checkpoint_threshold() == 0.8
    assert cfg.context_max_reincarnations() == 2
