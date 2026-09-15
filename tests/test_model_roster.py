from __future__ import annotations

from types import SimpleNamespace

import imp.providers.model_roster as mod
from imp.providers.claude_model_cache import ClaudeModelOption
from imp.providers.model_roster import ProviderRoster, discover_roster


def _config(tiers: dict) -> SimpleNamespace:
    return SimpleNamespace(providers=tuple(), tiers=tiers)


def test_lists_only_available_providers(monkeypatch) -> None:
    monkeypatch.setattr(mod, "available_providers", lambda config: ["anthropic"])
    tiers = {"high": {"anthropic": "claude-opus-4-8[1m]", "openai": "gpt-5.5"}}
    rosters = discover_roster(_config(tiers), claude_options=[])
    assert [r.provider for r in rosters] == ["anthropic"]  # openai filtered out


def test_unions_catalog_and_config_tier_models(monkeypatch) -> None:
    monkeypatch.setattr(mod, "available_providers", lambda config: ["anthropic"])
    # A tier names a model NOT in the catalog — it must still appear.
    tiers = {"high": {"anthropic": "made-up-model-x"}}
    rosters = discover_roster(_config(tiers), claude_options=[])
    names = list(rosters[0].entries)
    assert "claude-fable-5[1m]" in names          # from the catalog
    assert "made-up-model-x" in names             # from the config tiers block


def test_claude_cache_values_dedup(monkeypatch) -> None:
    monkeypatch.setattr(mod, "available_providers", lambda config: ["claude"])
    tiers = {"high": {"claude": "claude-opus-4-8[1m]"}}
    # A cache value already present via the catalog must appear exactly once.
    opts = [ClaudeModelOption(value="claude-fable-5[1m]")]
    rosters = discover_roster(_config(tiers), claude_options=opts)
    names = list(rosters[0].entries)
    assert names.count("claude-fable-5[1m]") == 1


def test_claude_cache_adds_new_values(monkeypatch) -> None:
    monkeypatch.setattr(mod, "available_providers", lambda config: ["claude"])
    tiers = {"high": {"claude": "claude-opus-4-8[1m]"}}
    opts = [ClaudeModelOption(value="cache-only-model")]
    rosters = discover_roster(_config(tiers), claude_options=opts)
    assert "cache-only-model" in list(rosters[0].entries)


def test_non_claude_provider_ignores_claude_options(monkeypatch) -> None:
    monkeypatch.setattr(mod, "available_providers", lambda config: ["anthropic"])
    tiers = {"high": {"anthropic": "claude-opus-4-8[1m]"}}
    opts = [ClaudeModelOption(value="cache-only-model")]
    rosters = discover_roster(_config(tiers), claude_options=opts)
    assert "cache-only-model" not in list(rosters[0].entries)


def test_shared_openai_catalog_exposes_astra_to_both_providers(monkeypatch) -> None:
    monkeypatch.setattr(mod, "available_providers", lambda config: ["openai", "codex"])
    rosters = discover_roster(_config({}), claude_options=[])
    assert [roster.provider for roster in rosters] == ["openai", "codex"]
    assert all("gpt-6-astra" in roster.entries for roster in rosters)


def test_returns_bare_string_entries(monkeypatch) -> None:
    monkeypatch.setattr(mod, "available_providers", lambda config: ["anthropic"])
    rosters = discover_roster(_config({}), claude_options=[])
    assert isinstance(rosters[0], ProviderRoster)
    assert all(isinstance(e, str) for e in rosters[0].entries)
