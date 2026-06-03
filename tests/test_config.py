from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.providers.config import load_config


def test_load_config_basic(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text(
        """
roles:
  default: {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}
  code:    {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}
  review:  {prefer: [{provider: anthropic, model: claude-opus-4-7}]}
""".strip()
    )
    cfg = load_config(tmp_path)
    assert "default" in cfg.roles
    assert cfg.roles["code"].prefer[0].provider == "anthropic"
    assert cfg.role_names() == frozenset({"default", "code", "review"})


def test_load_config_missing_default(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text(
        "roles:\n  code: {prefer: [{provider: anthropic, model: x}]}\n"
    )
    with pytest.raises(LoadError) as ei:
        load_config(tmp_path)
    assert "must define 'default' role" in str(ei.value)


def test_load_config_empty_prefer(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: []}\n"
    )
    with pytest.raises(LoadError) as ei:
        load_config(tmp_path)
    assert "prefer list" in str(ei.value)


def test_load_config_no_file(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    with pytest.raises(LoadError) as ei:
        load_config(tmp_path)
    assert "config.yaml not found" in str(ei.value)


def test_config_parses_model_rules(tmp_path):
    from imp.providers.config import load_config
    imp = tmp_path / "imp"
    imp.mkdir()
    (imp / "config.yaml").write_text(
        "model_rules:\n  code_review:\n    differ_provider_from: code\n"
        "roles:\n"
        "  default:\n    prefer:\n      - {provider: anthropic, model: m}\n")
    cfg = load_config(tmp_path)
    assert cfg.model_rules["code_review"].differ_provider_from == "code"


def test_config_model_rules_defaults_empty(tmp_path):
    from imp.providers.config import load_config
    imp = tmp_path / "imp"
    imp.mkdir()
    (imp / "config.yaml").write_text(
        "roles:\n"
        "  default:\n    prefer:\n      - {provider: anthropic, model: m}\n")
    cfg = load_config(tmp_path)
    assert cfg.model_rules == {}


def test_config_model_tiers_absent_is_none(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: [{provider: anthropic, model: m}]}\n"
    )
    cfg = load_config(tmp_path)
    assert cfg.model_tiers is None


def test_config_model_tiers_parsed(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text(
        "model_tiers: [balanced, powerful]\n"
        "roles:\n  default: {prefer: [{provider: anthropic, model: m}]}\n"
    )
    cfg = load_config(tmp_path)
    assert cfg.model_tiers == ("balanced", "powerful")


def test_config_model_tiers_empty_list_is_off(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text(
        "model_tiers: []\n"
        "roles:\n  default: {prefer: [{provider: anthropic, model: m}]}\n"
    )
    cfg = load_config(tmp_path)
    assert cfg.model_tiers == ()        # present-but-empty: off, not None


def test_config_model_tiers_duplicate_raises(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text(
        "model_tiers: [fast, fast]\n"
        "roles:\n  default: {prefer: [{provider: anthropic, model: m}]}\n"
    )
    with pytest.raises(LoadError, match="duplicate"):
        load_config(tmp_path)


def test_load_config_parses_lint_command(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text(
        "lint_command: \"make lint\"\n"
        "roles:\n"
        "  default: {prefer: [{provider: anthropic, model: m}]}\n"
    )
    cfg = load_config(tmp_path)
    assert cfg.lint_command == "make lint"


def test_load_config_lint_command_absent_is_none(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: [{provider: anthropic, model: m}]}\n"
    )
    cfg = load_config(tmp_path)
    assert cfg.lint_command is None


def test_load_config_lint_command_not_string_raises(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text(
        "lint_command: [make, lint]\n"
        "roles:\n  default: {prefer: [{provider: anthropic, model: m}]}\n"
    )
    with pytest.raises(LoadError, match="lint_command"):
        load_config(tmp_path)


def test_load_config_parses_test_command(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text(
        "test_command: \"make test\"\n"
        "roles:\n"
        "  default: {prefer: [{provider: anthropic, model: m}]}\n"
    )
    cfg = load_config(tmp_path)
    assert cfg.test_command == "make test"


def test_load_config_test_command_absent_is_none(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: [{provider: anthropic, model: m}]}\n"
    )
    cfg = load_config(tmp_path)
    assert cfg.test_command is None


def test_load_config_test_command_not_string_raises(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text(
        "test_command: [make, test]\n"
        "roles:\n  default: {prefer: [{provider: anthropic, model: m}]}\n"
    )
    with pytest.raises(LoadError, match="test_command"):
        load_config(tmp_path)


def test_editing_mode_defaults_to_emacs(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer:\n    - {provider: claude_code, model: opus}\n")
    cfg = load_config(tmp_path)
    assert cfg.editing_mode == "emacs"


def test_editing_mode_vi_accepted(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text(
        "editing_mode: vi\n"
        "roles:\n  default:\n    prefer:\n    - {provider: claude_code, model: opus}\n")
    cfg = load_config(tmp_path)
    assert cfg.editing_mode == "vi"


def test_editing_mode_rejects_unknown(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "config.yaml").write_text(
        "editing_mode: nano\n"
        "roles:\n  default:\n    prefer:\n    - {provider: claude_code, model: opus}\n")
    with pytest.raises(LoadError, match="editing_mode"):
        load_config(tmp_path)
