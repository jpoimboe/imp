# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""The two questions the per-quest provider pin raises, answered in one place so
an arming surface and the engine's reconcile cannot disagree about them."""
from __future__ import annotations

from typing import TYPE_CHECKING

from imp.providers.effective import load_effective_config
from imp.providers.resolve import available_providers

if TYPE_CHECKING:
    from imp.engine.quest import Quest
    from imp.engine.state import State


def current_provider(quest: Quest, state: State | None) -> str | None:
    """The vendor this quest runs on: its pin, else the vendor its resolved tier
    map names (uniform across tiers — resolve_run picks one winner for the whole
    map), else, before any resolve, what the effective config would pick now.
    Deliberately not resolve_current_model: a per-step /model override names
    another vendor entirely, and reading it would both refuse real switches and
    accept no-op ones. None only when nothing is available at all."""
    if quest.provider is not None:
        return quest.provider
    if state is not None and state.resolved_tiers is not None:
        return next(iter(state.resolved_tiers.values())).provider
    config = load_effective_config(control_root=quest.control_root).config
    return next(iter(available_providers(config)), None)


def provider_pending(quest: Quest, state: State) -> bool:
    """Whether a switch is waiting to be applied: the pin names a vendor the
    resolved tier map does not. A null pin is never pending, and a reconcile
    re-points the map, so this goes false for every reader at the same instant —
    there is no latch to clear."""
    if quest.provider is None or state.resolved_tiers is None:
        return False
    return any(pm.provider != quest.provider
               for pm in state.resolved_tiers.values())
