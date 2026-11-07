# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Discover the selectable-model roster for bare /model (spec: roster discovery).

For each env-available provider the roster is the de-duplicated union of:
  1. imp's catalog aliases for that provider,
  2. the model names the effective config's `tiers` block names for it, and
  3. for `claude` only: the values in Claude Code's local model cache.
Discovery is pure over its inputs — the caller reads the cache and passes it in —
so it needs no disk or network access. Each provider's bare model names are sorted
into family groups, most capable and newest first; the listing carries no descriptions."""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from imp.providers.catalog import catalog_aliases
from imp.providers.claude_model_cache import ClaudeModelOption
from imp.providers.resolve import available_providers

_OTHER = "Other"
_CLAUDE_FAMILIES = ("Fable", "Opus", "Sonnet", "Haiku")
_OPENAI_SIZES = ("Astra", "Sol", "Terra", "Luna")
_GPT_5 = "GPT-5"
_TOKEN_DELIMITER = re.compile(r"[-.\[]")
_VERSION_DELIMITER = re.compile(r"[-.]")
# ASCII only: int() raises on str.isdigit() characters like "²".
_VERSION_COMPONENT = re.compile(r"[0-9]{1,3}")


@dataclass(frozen=True, slots=True)
class ModelGroup:
    name: str
    models: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProviderRoster:
    provider: str
    groups: tuple[ModelGroup, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(name for group in self.groups for name in group.models)


def _claude_group(name: str) -> str:
    for group in _CLAUDE_FAMILIES:
        family = group.lower()
        full = f"claude-{family}"
        if (name in (family, full)
                or name.startswith((f"{family}[", f"{full}-", f"{full}["))):
            return group
    return _OTHER


def _openai_group(name: str) -> str:
    tokens = _TOKEN_DELIMITER.split(name)
    for group in _OPENAI_SIZES:
        if group.lower() in tokens:
            return group
    if name == "gpt-5" or name.startswith(("gpt-5.", "gpt-5-", "gpt-5[")):
        return _GPT_5
    return _OTHER


_Scheme = tuple[tuple[str, ...], Callable[[str], str]]
_CLAUDE_SCHEME: _Scheme = (_CLAUDE_FAMILIES, _claude_group)
_OPENAI_SCHEME: _Scheme = ((*_OPENAI_SIZES, _GPT_5), _openai_group)
_SCHEMES: dict[str, _Scheme] = {
    "claude": _CLAUDE_SCHEME,
    "anthropic": _CLAUDE_SCHEME,
    "openai": _OPENAI_SCHEME,
    "codex": _OPENAI_SCHEME,
}
_NO_SCHEME: _Scheme = ((), lambda name: _OTHER)


def _version(name: str) -> tuple[int, ...]:
    run: list[int] = []
    for part in _VERSION_DELIMITER.split(name.partition("[")[0]):
        if _VERSION_COMPONENT.fullmatch(part):
            run.append(int(part))
        elif run:
            break
    return tuple(run)


def group_models(provider: str, names: Iterable[str]) -> tuple[ModelGroup, ...]:
    order, classify = _SCHEMES.get(provider, _NO_SCHEME)
    buckets: dict[str, list[str]] = {}
    for name in sorted(set(names)):
        buckets.setdefault(classify(name), []).append(name)
    # Tuples sort a longer version above its prefix and () last; reverse=True is stable.
    groups = [
        ModelGroup(group, tuple(sorted(buckets[group], key=_version, reverse=True)))
        for group in order if group in buckets
    ]
    if _OTHER in buckets:
        groups.append(ModelGroup(_OTHER, tuple(buckets[_OTHER])))
    return tuple(groups)


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
        rosters.append(ProviderRoster(provider, group_models(provider, names)))
    return rosters
