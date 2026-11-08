# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.providers import (
    _LOGIN_FOUND,
    BUILTIN_FLOOR,
    ProviderStatus,
    Readiness,
    provider_readiness,
    readiness_lines,
)
from imp.providers.config import Config
from tests._readiness import pin_readiness

_SIGNED_IN = ProviderStatus(Readiness.SIGNED_IN, "signed in")
_NOT_SIGNED_IN = ProviderStatus(Readiness.NOT_SIGNED_IN, "not signed in")
_CLI_NOT_FOUND = ProviderStatus(Readiness.CLI_NOT_FOUND, "CLI not found")
_OAUTH = {"claudeAiOauth": {"accessToken": "a", "expiresAt": 1}}


@pytest.fixture(autouse=True)
def _pinned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pin_readiness(monkeypatch, tmp_path)


def test_a_cli_missing_from_path_reads_cli_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    pin_readiness(monkeypatch, tmp_path, on_path=())
    assert provider_readiness("claude", Config()) == _CLI_NOT_FOUND
    assert provider_readiness("codex", Config()) == _CLI_NOT_FOUND


def test_a_cli_on_path_without_a_login_reads_not_signed_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    pin_readiness(monkeypatch, tmp_path, keys=())
    assert provider_readiness("claude", Config()) == _NOT_SIGNED_IN
    assert provider_readiness("codex", Config()) == _NOT_SIGNED_IN


@pytest.mark.parametrize(("body", "expected"), [
    (json.dumps(_OAUTH), _SIGNED_IN),
    (json.dumps({"other": {}}), _NOT_SIGNED_IN),
    (json.dumps({"claudeAiOauth": "not-an-object"}), _NOT_SIGNED_IN),
    ("{", _NOT_SIGNED_IN),
])
def test_claude_reads_claude_ai_oauth_from_its_credentials_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str, expected: ProviderStatus,
) -> None:
    claude_dir, _ = pin_readiness(monkeypatch, tmp_path, keys=())
    (claude_dir / ".credentials.json").write_text(body)
    assert provider_readiness("claude", Config()) == expected


@pytest.mark.parametrize("var", ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"])
def test_claude_counts_an_env_login_without_a_credentials_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, var: str,
) -> None:
    pin_readiness(monkeypatch, tmp_path, keys=(var,))
    assert provider_readiness("claude", Config()) == _SIGNED_IN


@pytest.mark.parametrize(("auth", "keys"), [
    ({"auth_mode": "chatgpt", "tokens": {"access_token": "a"}}, ()),
    (None, ("OPENAI_API_KEY",)),
])
def test_codex_counts_a_detected_auth_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    auth: dict[str, object] | None, keys: tuple[str, ...],
) -> None:
    _, codex_dir = pin_readiness(monkeypatch, tmp_path, keys=keys)
    if auth is not None:
        (codex_dir / "auth.json").write_text(json.dumps(auth))
    assert provider_readiness("codex", Config()) == _SIGNED_IN


def _stub_keychain(monkeypatch: pytest.MonkeyPatch, value: str | None) -> list[None]:
    from claude_agent_sdk._internal import session_resume

    calls: list[None] = []

    def _read() -> str | None:
        calls.append(None)
        return value

    monkeypatch.setattr(session_resume, "_read_keychain_credentials", _read)
    return calls


def _default_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


@pytest.mark.parametrize(("keychain", "expected"), [
    (json.dumps(_OAUTH), _SIGNED_IN),
    (json.dumps({"other": {}}), _NOT_SIGNED_IN),
    (None, _NOT_SIGNED_IN),
])
def test_macos_reads_the_keychain_for_the_default_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    keychain: str | None, expected: ProviderStatus,
) -> None:
    pin_readiness(monkeypatch, tmp_path, keys=())
    _default_config_dir(monkeypatch, tmp_path)
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    calls = _stub_keychain(monkeypatch, keychain)
    assert provider_readiness("claude", Config()) == expected
    assert len(calls) == 1


def test_a_non_utf8_credentials_file_reads_not_signed_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    claude_dir, _ = pin_readiness(monkeypatch, tmp_path, keys=())
    monkeypatch.setattr("platform.system", lambda: "Linux")
    (claude_dir / ".credentials.json").write_bytes(b"\xff\xfe\x00bad")
    assert provider_readiness("claude", Config()) == _NOT_SIGNED_IN


def _no_home() -> Path:
    raise RuntimeError("no home")


def test_an_unresolvable_home_directory_reads_not_signed_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    pin_readiness(monkeypatch, tmp_path, keys=())
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.delenv("CODEX_HOME")
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr(Path, "home", staticmethod(_no_home))
    assert provider_readiness("claude", Config()) == _NOT_SIGNED_IN
    assert provider_readiness("codex", Config()) == _NOT_SIGNED_IN
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert provider_readiness("codex", Config()) == _SIGNED_IN


@pytest.mark.parametrize(("system", "override"), [("Linux", False), ("Darwin", True)])
def test_the_keychain_is_skipped_off_macos_and_under_a_config_dir_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, system: str, override: bool,
) -> None:
    pin_readiness(monkeypatch, tmp_path, keys=())
    if not override:
        _default_config_dir(monkeypatch, tmp_path)
    monkeypatch.setattr("platform.system", lambda: system)
    calls = _stub_keychain(monkeypatch, json.dumps(_OAUTH))
    assert provider_readiness("claude", Config()) == _NOT_SIGNED_IN
    assert calls == []


def test_an_override_makes_a_login_builtin_need_its_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    pin_readiness(monkeypatch, tmp_path, on_path=())
    cfg = Config(provider_envs={"claude": "MY_CLAUDE_KEY"})
    monkeypatch.delenv("MY_CLAUDE_KEY", raising=False)
    assert provider_readiness("claude", cfg) == ProviderStatus(
        Readiness.KEY_UNSET, "MY_CLAUDE_KEY not set")
    monkeypatch.setenv("MY_CLAUDE_KEY", "k")
    assert provider_readiness("claude", cfg) == ProviderStatus(
        Readiness.KEY_SET, "MY_CLAUDE_KEY set")


def test_a_key_provider_reads_set_or_not_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    pin_readiness(monkeypatch, tmp_path, keys=("ANTHROPIC_API_KEY",))
    assert provider_readiness("anthropic", Config()) == ProviderStatus(
        Readiness.KEY_SET, "ANTHROPIC_API_KEY set")
    assert provider_readiness("openai", Config()) == ProviderStatus(
        Readiness.KEY_UNSET, "OPENAI_API_KEY not set")


def test_a_key_provider_reads_its_configured_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = Config(provider_envs={"anthropic": "MY_KEY"})
    monkeypatch.delenv("MY_KEY", raising=False)
    assert provider_readiness("anthropic", cfg) == ProviderStatus(
        Readiness.KEY_UNSET, "MY_KEY not set")
    monkeypatch.setenv("MY_KEY", "k")
    assert provider_readiness("anthropic", cfg) == ProviderStatus(
        Readiness.KEY_SET, "MY_KEY set")


def test_an_empty_key_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert provider_readiness("anthropic", Config()).readiness is Readiness.KEY_UNSET


def test_a_name_outside_the_floor_reads_its_derived_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ACME_API_KEY", raising=False)
    assert provider_readiness("acme", Config()) == ProviderStatus(
        Readiness.KEY_UNSET, "ACME_API_KEY not set")
    monkeypatch.setenv("ACME_API_KEY", "k")
    assert provider_readiness("acme", Config()).readiness is Readiness.KEY_SET


def test_only_signed_in_and_key_set_are_ready() -> None:
    assert {r for r in Readiness if r.ready} == {Readiness.SIGNED_IN, Readiness.KEY_SET}


def test_every_floor_login_cli_has_a_login_check() -> None:
    assert set(_LOGIN_FOUND) == {n for n, e in BUILTIN_FLOOR.items() if e.env_var is None}


def test_the_readiness_rule_has_no_copy_in_the_cli() -> None:
    cli = Path(__file__).resolve().parents[1] / "src" / "imp" / "cli"
    for name in ("config_cmd.py", "init.py"):
        text = (cli / name).read_text()
        assert "env_var is None" not in text, name
        assert "provider_envs" not in text, name


def test_readiness_lines_name_the_fix_for_each_not_ready_floor_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    pin_readiness(monkeypatch, tmp_path, on_path=("claude",), keys=())
    assert readiness_lines(
        ["housecat", "claude", "codex", "anthropic", "openai"], Config()) == [
        "  claude — not signed in · run `claude login`",
        "  codex — CLI not found · install it, then run `codex login`",
        "  anthropic — ANTHROPIC_API_KEY not set",
        "  openai — OPENAI_API_KEY not set",
    ]


def test_readiness_lines_print_nothing_for_ready_providers() -> None:
    assert readiness_lines(["claude", "codex", "anthropic", "openai"], Config()) == []


def test_floor_config_carries_only_the_builtin_key_names() -> None:
    from imp.providers.effective import floor_config
    from imp.providers.paths import global_config_path

    cfg = global_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("provider_envs: {anthropic: {env: MY_KEY}}\n")

    assert floor_config().provider_envs == {
        "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
