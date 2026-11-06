from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace

import pytest

import imp.providers.model_roster as mod
from imp.providers.claude_model_cache import ClaudeModelOption
from imp.providers.model_roster import ModelGroup, ProviderRoster, discover_roster, group_models


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


def _flat(provider: str, names: Iterable[str]) -> list[str]:
    return [name for group in group_models(provider, names) for name in group.models]


@pytest.mark.parametrize(("provider", "ordered"), [
    ("claude", ("claude-opus-5-5", "claude-opus-5-5[1m]", "claude-opus-5", "claude-opus-4-8")),
    ("claude", ("claude-fable-5-1[1m]", "claude-fable-5")),
    ("openai", ("gpt-5.4", "gpt-5.4-mini")),
    ("claude", ("claude-haiku-4-5", "claude-haiku-4-5-20251001", "claude-haiku-4-5[1m]")),
    ("openai", ("gpt-6.1-sol", "gpt-5.6-sol")),
    ("openai", ("gpt-5.3-codex", "gpt-5", "gpt-5-2025-08-07")),
    ("openai", ("gpt-5.4-mini", "gpt-5.4-mini-2", "gpt-5.3-codex")),
    ("claude", ("claude-opus-4-8", "opus", "opus[1m]")),
    ("openai", ("gpt-5.10", "gpt-5.9")),
    ("claude", ("claude-opus-5-10", "claude-opus-5-9")),
    ("openai", ("gpt-5.100", "gpt-5.99")),
])
def test_a_group_sorts_highest_version_then_alphabetically(
    provider: str, ordered: tuple[str, ...],
) -> None:
    assert _flat(provider, reversed(ordered)) == list(ordered)


@pytest.mark.parametrize("provider", ["claude", "anthropic"])
@pytest.mark.parametrize(("name", "group"), [
    ("claude-fable-5", "Fable"),
    ("claude-opus-5[1m]", "Opus"),
    ("claude-sonnet-4-6", "Sonnet"),
    ("claude-haiku-4-5-20251001", "Haiku"),
    ("claude-opus", "Opus"),
    ("claude-opus[1m]", "Opus"),
    ("opus", "Opus"),
    ("sonnet[1m]", "Sonnet"),
    ("claude-3-5-sonnet-20241022", "Other"),
    ("claude-opusx-1", "Other"),
    ("opus-4", "Other"),
    ("gpt-6-astra", "Other"),
])
def test_claude_classification(provider: str, name: str, group: str) -> None:
    assert group_models(provider, [name]) == (ModelGroup(group, (name,)),)


@pytest.mark.parametrize("provider", ["openai", "codex"])
@pytest.mark.parametrize(("name", "group"), [
    ("gpt-6-astra", "Astra"),
    ("gpt-6.1-sol", "Sol"),
    ("gpt-5.6-terra", "Terra"),
    ("gpt-6-luna", "Luna"),
    ("sol", "Sol"),
    ("gpt-6.sol", "Sol"),
    ("gpt-6-astra[1m]", "Astra"),
    ("gpt-sol-luna", "Sol"),
    ("gpt-5", "GPT-5"),
    ("gpt-5.6", "GPT-5"),
    ("gpt-5-2025-08-07", "GPT-5"),
    ("gpt-5[1m]", "GPT-5"),
    ("gpt-5.4-solar", "GPT-5"),
    ("gpt-50", "Other"),
    ("gpt-6", "Other"),
    ("consol-1", "Other"),
    ("claude-opus-5", "Other"),
])
def test_openai_classification(provider: str, name: str, group: str) -> None:
    assert group_models(provider, [name]) == (ModelGroup(group, (name,)),)


def test_groups_run_in_family_order_with_other_last() -> None:
    claude = group_models("claude", [
        "foo", "claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-5", "claude-fable-5"])
    assert [g.name for g in claude] == ["Fable", "Opus", "Sonnet", "Haiku", "Other"]
    openai = group_models("codex", [
        "foo", "gpt-5", "gpt-6-luna", "gpt-6.1-terra", "gpt-6.1-sol", "gpt-6-astra"])
    assert [g.name for g in openai] == ["Astra", "Sol", "Terra", "Luna", "GPT-5", "Other"]


def test_other_sorts_alphabetically_ignoring_versions() -> None:
    assert group_models("claude", ["zeta-9", "claude-3-5-sonnet-20241022", "alpha-2"]) == (
        ModelGroup("Other", ("alpha-2", "claude-3-5-sonnet-20241022", "zeta-9")),)


def test_a_provider_with_no_family_table_is_all_other() -> None:
    assert group_models("scripted", ["gpt-6-astra", "claude-opus-5", "a"]) == (
        ModelGroup("Other", ("a", "claude-opus-5", "gpt-6-astra")),)


def test_no_name_is_dropped_and_duplicates_collapse() -> None:
    names = ["claude-3-5-sonnet-20241022", "gpt-50", "gpt-6", "consol-1", "opus", "claude-fable-5"]
    assert sorted(_flat("claude", [*names, "opus"])) == sorted(names)


def test_a_non_ascii_digit_is_not_a_version_component() -> None:
    assert _flat("openai", ["gpt-5.²", "gpt-5.1"]) == ["gpt-5.1", "gpt-5.²"]
