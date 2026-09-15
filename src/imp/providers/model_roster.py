# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Discover the selectable-model roster for bare /model (spec: roster discovery).

For each env-available provider the roster is the de-duplicated union of:
  1. imp's catalog aliases for that provider,
  2. the model names the effective config's `tiers` block names for it, and
  3. for `claude` only: the values in Claude Code's local model cache.
Discovery is pure over its inputs — the caller reads the cache and passes it in —
so it needs no disk or network access. Entries are bare model-name strings; the
listing carries no descriptions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from imp.providers.catalog import catalog_aliases
from imp.providers.claude_model_cache import ClaudeModelOption
from imp.providers.resolve import available_providers


@dataclass(frozen=True, slots=True)
class ProviderRoster:
    provider: str
    entries: tuple[str, ...]


def discover_roster(
    config: Any, *, claude_options: list[ClaudeModelOption],
) -> list[ProviderRoster]:
    rosters: list[ProviderRoster] = []
    for provider in available_providers(config):
        names = list(catalog_aliases(provider))
        for vendor_map in config.tiers.values():
            model = vendor_map.get(provider)
            if model is not None:
                names.append(model)
        if provider == "claude":
            names.extend(opt.value for opt in claude_options)
        order: list[str] = []
        for name in names:
            if name not in order:
                order.append(name)
        rosters.append(ProviderRoster(provider, tuple(order)))
    return rosters
