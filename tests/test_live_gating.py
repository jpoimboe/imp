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


from tests._live import RUN_LIVE_ALL as _ALL  # noqa: E402


class _StubItem:
    """Minimal stand-in for pytest.Item for exercising the collection hook."""

    def __init__(self, fspath, live_provider=None):
        self.fspath = fspath
        self._markers = {}
        if live_provider is not None:
            self._markers["live"] = pytest.mark.live(live_provider).mark
        self.added = []

    def get_closest_marker(self, name):
        for deco in self.added:
            if deco.mark.name == name:
                return deco.mark
        return self._markers.get(name)

    def add_marker(self, deco):
        self.added.append(deco)


class _StubConfig:
    def __init__(self, run_live):
        self._run_live = run_live

    def getoption(self, name):
        assert name == "--run-live"
        return self._run_live


def _skip_reasons(item):
    return [d.mark.kwargs["reason"] for d in item.added if d.mark.name == "skip"]


def test_hook_no_flag_skips_live_marked_test():
    from tests.conftest import pytest_collection_modifyitems
    item = _StubItem("tests/test_x_live.py", live_provider="anthropic")
    pytest_collection_modifyitems(_StubConfig(None), [item])
    assert "live-API test — pass --run-live to run" in _skip_reasons(item)


def test_hook_adds_raised_timeout_to_live_file():
    from tests.conftest import _LIVE_TEST_TIMEOUT, pytest_collection_modifyitems
    item = _StubItem("tests/test_x_live.py", live_provider="anthropic")
    pytest_collection_modifyitems(_StubConfig(None), [item])
    timeouts = [d for d in item.added if d.mark.name == "timeout"]
    assert timeouts and timeouts[0].mark.args[0] == _LIVE_TEST_TIMEOUT


def test_hook_runs_selected_available_provider(monkeypatch):
    import tests._live as live
    from tests.conftest import pytest_collection_modifyitems
    monkeypatch.setattr(live, "provider_available", lambda _p: None)
    item = _StubItem("tests/test_x_live.py", live_provider="claude_code")
    pytest_collection_modifyitems(_StubConfig(_ALL), [item])
    assert _skip_reasons(item) == []


def test_hook_ignores_non_live_item():
    from tests.conftest import pytest_collection_modifyitems
    item = _StubItem("tests/test_ordinary.py", live_provider=None)
    pytest_collection_modifyitems(_StubConfig(None), [item])
    assert item.added == []


def test_hook_unknown_provider_flag_raises_usage_error():
    from tests.conftest import pytest_collection_modifyitems
    item = _StubItem("tests/test_x_live.py", live_provider="anthropic")
    with pytest.raises(pytest.UsageError):
        pytest_collection_modifyitems(_StubConfig("bogus"), [item])
