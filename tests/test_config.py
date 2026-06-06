from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.providers.config import load_config


def _write(tmp_path: Path, body: str) -> Path:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text(body)
    return tmp_path


_OK_BODY = (
    "providers: [claude_code, openai]\n"
    "tiers:\n"
    "  low: {claude_code: haiku, openai: gpt-5.4-mini}\n"
    "  medium: {claude_code: sonnet, openai: gpt-5.3-codex}\n"
    "  high: {claude_code: opus, openai: gpt-5.5}\n"
)


def test_load_config_basic(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _OK_BODY))
    assert cfg.providers == ("claude_code", "openai")
    assert set(cfg.tiers) == {"low", "medium", "high"}
    assert cfg.tiers["medium"]["openai"] == "gpt-5.3-codex"
    assert cfg.tier_names() == frozenset({"low", "medium", "high"})


def test_theme_defaults_to_ansi_when_absent(tmp_path: Path) -> None:
    # Pre-theme configs (no `theme:` key) load as the universal 'ansi'.
    assert load_config(_write(tmp_path, _OK_BODY)).theme == "ansi"


def test_theme_parsed_and_validated(tmp_path: Path) -> None:
    for sub in ("d", "l", "x"):
        (tmp_path / sub).mkdir()
    assert load_config(_write(tmp_path / "d", _OK_BODY + "theme: dark\n")).theme == "dark"
    assert load_config(_write(tmp_path / "l", _OK_BODY + "theme: light\n")).theme == "light"
    with pytest.raises(LoadError, match="theme"):
        load_config(_write(tmp_path / "x", _OK_BODY + "theme: neon\n"))


def test_load_config_missing_providers(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="providers"):
        load_config(_write(tmp_path,
            "tiers:\n"
            "  low: {claude_code: haiku}\n"
            "  medium: {claude_code: sonnet}\n"
            "  high: {claude_code: opus}\n"))


def test_load_config_missing_tier_is_error(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match=r"tiers.*missing"):
        load_config(_write(tmp_path,
            "providers: [claude_code]\n"
            "tiers:\n"
            "  low: {claude_code: haiku}\n"
            "  medium: {claude_code: sonnet}\n"))


def test_load_config_no_file(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    with pytest.raises(LoadError, match=r"config\.yaml not found"):
        load_config(tmp_path)


def test_load_config_unknown_tier_rejected(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="tiers:"):
        load_config(_write(tmp_path,
            "providers: [claude_code]\n"
            "tiers:\n"
            "  low: {claude_code: haiku}\n"
            "  medium: {claude_code: sonnet}\n"
            "  high: {claude_code: opus}\n"
            "  extra: {claude_code: opus}\n"))


def test_load_config_provider_envs_default_inferred(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _OK_BODY))
    assert cfg.env_for("openai") == "OPENAI_API_KEY"
    assert cfg.env_for("anthropic") == "ANTHROPIC_API_KEY"
    assert cfg.provider_envs == {}


def test_load_config_provider_envs_override(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path,
        "provider_envs:\n  openai: {env: OAI_KEY}\n" + _OK_BODY))
    assert cfg.env_for("openai") == "OAI_KEY"
    assert cfg.env_for("claude_code") == "CLAUDE_CODE_API_KEY"   # default


def test_load_config_parses_lint_command(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path,
        "lint_command: \"make lint\"\n" + _OK_BODY))
    assert cfg.lint_command == "make lint"


def test_load_config_lint_command_absent_is_none(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _OK_BODY))
    assert cfg.lint_command is None


def test_load_config_lint_command_not_string_raises(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="lint_command"):
        load_config(_write(tmp_path,
            "lint_command: [make, lint]\n" + _OK_BODY))


def test_load_config_parses_test_command(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path,
        "test_command: \"make test\"\n" + _OK_BODY))
    assert cfg.test_command == "make test"


def test_load_config_test_command_absent_is_none(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _OK_BODY))
    assert cfg.test_command is None


def test_load_config_test_command_not_string_raises(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="test_command"):
        load_config(_write(tmp_path,
            "test_command: [make, test]\n" + _OK_BODY))


def test_editing_mode_defaults_to_emacs(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _OK_BODY))
    assert cfg.editing_mode == "emacs"


def test_editing_mode_vi_accepted(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path,
        "editing_mode: vi\n" + _OK_BODY))
    assert cfg.editing_mode == "vi"


def test_editing_mode_rejects_unknown(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="editing_mode"):
        load_config(_write(tmp_path,
            "editing_mode: nano\n" + _OK_BODY))


def test_providers_duplicate_rejected(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="duplicate"):
        load_config(_write(tmp_path,
            "providers: [openai, openai]\n"
            "tiers:\n"
            "  low: {openai: gpt-5.4-mini}\n"
            "  medium: {openai: gpt-5.3-codex}\n"
            "  high: {openai: gpt-5.5}\n"))
