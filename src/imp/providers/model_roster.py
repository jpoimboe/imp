"""Discover the selectable-model roster for bare /model (spec: roster discovery).

For each env-available provider the roster is the de-duplicated union of:
  1. imp's catalog aliases for that provider,
  2. the model names the effective config's `tiers` block names for it, and
  3. for `claude_code` only: the entries in Claude Code's local model cache.
Only the Claude cache carries a human description; catalog/config names list
plain. Discovery is pure over its inputs — the caller reads the cache and passes
it in — so it needs no disk or network access."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from imp.providers.catalog import catalog_aliases
from imp.providers.claude_model_cache import ClaudeModelOption
from imp.providers.resolve import available_providers


@dataclass(frozen=True, slots=True)
class RosterEntry:
    name: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderRoster:
    provider: str
    entries: tuple[RosterEntry, ...]


def _describe(opt: ClaudeModelOption) -> str | None:
    parts = [p for p in (opt.label, opt.description) if p]
    return " · ".join(parts) if parts else None


def discover_roster(
    config: Any, *, claude_options: list[ClaudeModelOption],
) -> list[ProviderRoster]:
    rosters: list[ProviderRoster] = []
    for provider in available_providers(config):
        order: list[str] = []
        descriptions: dict[str, str] = {}
        names = list(catalog_aliases(provider))
        for vendor_map in config.tiers.values():
            model = vendor_map.get(provider)
            if model is not None:
                names.append(model)
        for name in names:
            if name not in order:
                order.append(name)
        if provider == "claude_code":
            for opt in claude_options:
                if opt.value not in order:
                    order.append(opt.value)
                desc = _describe(opt)
                if desc is not None:
                    descriptions[opt.value] = desc
        entries = tuple(
            RosterEntry(name, descriptions.get(name)) for name in order)
        rosters.append(ProviderRoster(provider, entries))
    return rosters
