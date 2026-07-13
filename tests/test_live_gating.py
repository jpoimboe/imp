"""Unit tests for the --run-live gating mechanism in tests/_live.py and the
tests/conftest.py collection hook. These are ordinary (non-live) unit tests —
the `test_live_` filename prefix keeps them out of the live-file convention."""
from __future__ import annotations

import pytest

from tests._live import KNOWN_PROVIDERS, RUN_LIVE_ALL, parse_run_live


def test_parse_run_live_absent_returns_none():
    assert parse_run_live(None) is None


def test_parse_run_live_bare_returns_all_providers():
    assert parse_run_live(RUN_LIVE_ALL) == frozenset(KNOWN_PROVIDERS)
    assert set(KNOWN_PROVIDERS) == {"anthropic", "openai", "claude_code", "codex"}


def test_parse_run_live_subset_parses_named_providers():
    assert parse_run_live("claude_code,openai") == frozenset({"claude_code", "openai"})


def test_parse_run_live_unknown_provider_raises_usage_error():
    with pytest.raises(pytest.UsageError) as exc:
        parse_run_live("bogus")
    msg = str(exc.value)
    assert "bogus" in msg
    assert all(p in msg for p in KNOWN_PROVIDERS)  # names the valid providers


def test_anthropic_key_prefers_key_file_over_env(monkeypatch, tmp_path):
    (tmp_path / ".anthropic-key").write_text("file-key\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    from tests._live import anthropic_key
    assert anthropic_key() == "file-key"


def test_anthropic_key_falls_back_to_env_when_no_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.anthropic-key present
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    from tests._live import anthropic_key
    assert anthropic_key() == "env-key"


def test_anthropic_key_none_when_neither(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from tests._live import anthropic_key
    assert anthropic_key() is None


def test_openai_key_prefers_key_file_over_env(monkeypatch, tmp_path):
    (tmp_path / ".openai-key").write_text("file-key\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    from tests._live import openai_key
    assert openai_key() == "file-key"


def test_provider_available_claude_code_missing_cli(monkeypatch):
    import tests._live as live
    monkeypatch.setattr(live.shutil, "which", lambda _name: None)
    assert live.provider_available("claude_code") == "claude CLI not on PATH"


def test_provider_available_claude_code_present(monkeypatch):
    import tests._live as live
    monkeypatch.setattr(live.shutil, "which", lambda _name: "/usr/bin/claude")
    assert live.provider_available("claude_code") is None


def test_provider_available_codex_missing_sdk(monkeypatch):
    import tests._live as live
    monkeypatch.setattr(live.shutil, "which", lambda _name: "/usr/bin/codex")
    monkeypatch.setattr(live.importlib.util, "find_spec", lambda _name: None)
    assert live.provider_available("codex") == "openai_codex_sdk not importable"


def test_provider_available_anthropic_missing_key(monkeypatch):
    import tests._live as live
    monkeypatch.setattr(live, "anthropic_key", lambda: None)
    reason = live.provider_available("anthropic")
    assert reason is not None and "anthropic" in reason.lower() and "ANTHROPIC_API_KEY" in reason


def test_provider_available_anthropic_present(monkeypatch):
    import tests._live as live
    monkeypatch.setattr(live, "anthropic_key", lambda: "real-key")
    assert live.provider_available("anthropic") is None


def test_live_skip_reason_no_flag_skips_all():
    from tests._live import live_skip_reason
    assert live_skip_reason(None, "anthropic") == "live-API test — pass --run-live to run"


def test_live_skip_reason_provider_not_selected():
    from tests._live import live_skip_reason
    reason = live_skip_reason(frozenset({"openai"}), "anthropic")
    assert reason == "anthropic not selected by --run-live"


def test_live_skip_reason_selected_but_unavailable(monkeypatch):
    import tests._live as live
    monkeypatch.setattr(live, "provider_available", lambda _p: "claude CLI not on PATH")
    assert (
        live.live_skip_reason(frozenset({"claude_code"}), "claude_code")
        == "claude CLI not on PATH"
    )


def test_live_skip_reason_selected_and_available_runs(monkeypatch):
    import tests._live as live
    monkeypatch.setattr(live, "provider_available", lambda _p: None)
    assert live.live_skip_reason(frozenset({"anthropic"}), "anthropic") is None
