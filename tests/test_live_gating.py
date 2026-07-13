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
