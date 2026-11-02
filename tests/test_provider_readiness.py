# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
from __future__ import annotations

from pathlib import Path

import pytest

from imp.providers import Readiness, provider_readiness, readiness_lines
from imp.providers.config import Config


def test_a_login_builtin_without_an_override_is_cli_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    assert provider_readiness("claude", Config()) is Readiness.CLI_AUTHENTICATED
    assert provider_readiness("codex", Config()) is Readiness.CLI_AUTHENTICATED


def test_an_override_makes_a_login_builtin_need_its_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = Config(provider_envs={"claude": "MY_CLAUDE_KEY"})
    monkeypatch.delenv("MY_CLAUDE_KEY", raising=False)
    assert provider_readiness("claude", cfg) is Readiness.KEY_UNSET
    monkeypatch.setenv("MY_CLAUDE_KEY", "k")
    assert provider_readiness("claude", cfg) is Readiness.KEY_SET


def test_a_key_provider_reads_its_configured_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = Config(provider_envs={"anthropic": "MY_KEY"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("MY_KEY", raising=False)
    assert provider_readiness("anthropic", cfg) is Readiness.KEY_UNSET
    monkeypatch.setenv("MY_KEY", "k")
    assert provider_readiness("anthropic", cfg) is Readiness.KEY_SET


def test_an_empty_key_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert provider_readiness("anthropic", Config()) is Readiness.KEY_UNSET


def test_a_name_outside_the_floor_reads_its_derived_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ACME_API_KEY", raising=False)
    assert provider_readiness("acme", Config()) is Readiness.KEY_UNSET
    monkeypatch.setenv("ACME_API_KEY", "k")
    assert provider_readiness("acme", Config()) is Readiness.KEY_SET


def test_only_an_unset_key_is_not_ready() -> None:
    assert Readiness.CLI_AUTHENTICATED.ready
    assert Readiness.KEY_SET.ready
    assert not Readiness.KEY_UNSET.ready


def test_the_readiness_rule_has_no_copy_in_the_cli() -> None:
    cli = Path(__file__).resolve().parents[1] / "src" / "imp" / "cli"
    for name in ("config_cmd.py", "init.py"):
        text = (cli / name).read_text()
        assert "env_var is None" not in text, name
        assert "provider_envs" not in text, name


def test_readiness_lines_mark_ready_or_hint_and_skip_unknown_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert readiness_lines(["housecat", "claude", "anthropic", "openai"], Config()) == [
        "  claude — ready",
        "  anthropic — ready",
        "  openai — set OPENAI_API_KEY",
    ]


def test_floor_config_carries_only_the_builtin_key_names() -> None:
    from imp.providers.effective import floor_config
    from imp.providers.paths import global_config_path

    cfg = global_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("provider_envs: {anthropic: {env: MY_KEY}}\n")

    assert floor_config().provider_envs == {
        "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
