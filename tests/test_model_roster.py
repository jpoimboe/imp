from __future__ import annotations

from types import SimpleNamespace

import imp.providers.model_roster as mod
from imp.providers.claude_model_cache import ClaudeModelOption
from imp.providers.model_roster import (
    ProviderRoster,
    RosterEntry,
    discover_roster,
)


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
    names = [e.name for e in rosters[0].entries]
    assert "claude-fable-5[1m]" in names          # from the catalog
    assert "made-up-model-x" in names             # from the config tiers block
    # Catalog/config entries carry no description.
    assert all(e.description is None for e in rosters[0].entries)


def test_claude_cache_attaches_description_and_dedups(monkeypatch) -> None:
    monkeypatch.setattr(mod, "available_providers", lambda config: ["claude_code"])
    tiers = {"high": {"claude_code": "claude-opus-4-8[1m]"}}
    # The cache repeats an alias already in the catalog and adds a description.
    opts = [ClaudeModelOption(
        value="claude-fable-5[1m]", label="Fable 5",
        description="Most capable")]
    rosters = discover_roster(_config(tiers), claude_options=opts)
    entries = rosters[0].entries
    fable = [e for e in entries if e.name == "claude-fable-5[1m]"]
    assert len(fable) == 1                                   # de-duplicated
    assert fable[0].description == "Fable 5 · Most capable"  # label · description


def test_non_claude_provider_ignores_claude_options(monkeypatch) -> None:
    monkeypatch.setattr(mod, "available_providers", lambda config: ["anthropic"])
    tiers = {"high": {"anthropic": "claude-opus-4-8[1m]"}}
    opts = [ClaudeModelOption(value="cache-only-model", label="X")]
    rosters = discover_roster(_config(tiers), claude_options=opts)
    names = [e.name for e in rosters[0].entries]
    assert "cache-only-model" not in names


def test_returns_structured_rosters(monkeypatch) -> None:
    monkeypatch.setattr(mod, "available_providers", lambda config: ["anthropic"])
    rosters = discover_roster(_config({}), claude_options=[])
    assert isinstance(rosters[0], ProviderRoster)
    assert all(isinstance(e, RosterEntry) for e in rosters[0].entries)
