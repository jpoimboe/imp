# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Current-and-roster derivation for the provider pin, shared by the `p` picker
and both `/provider` worlds so no two arming surfaces can disagree about what is
current or what may be picked."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

PICKER_HEAD = (
    "switch provider — the rest of this quest runs on your pick",
    "takes effect right away — this step is waiting to retry",
)
PICKER_FOOT = "press a number, or esc to cancel"
ROSTER_FOOT = "Switch with  /provider <name> — the rest of this quest runs on it."


def selectable_available() -> list[str]:
    """The names every arming surface offers: floor entries carrying a full tier
    table whose env_check passes, in floor order. Both env_check forms are local
    reads, so this is computed in the client process."""
    from imp.providers import selectable_providers
    from imp.providers.resolve import provider_is_available
    return [n for n in selectable_providers() if provider_is_available(n)]


def render_picker(current: str | None, names: list[str]) -> list[str]:
    lines = [*PICKER_HEAD, ""]
    for i, name in enumerate(names, 1):
        lines.append(f"  {i}  {name}   current" if name == current
                     else f"  {i}  {name}")
    return [*lines, "", PICKER_FOOT]


def picker_choices(current: str | None, names: list[str]) -> list[str | None]:
    """What each digit dispatches, the current vendor's slot None: a re-pick has
    no name to send, so it cannot arm a switch no reconcile would clear."""
    return [None if n == current else n for n in names]


def render_provider_roster(current: str | None, names: list[str]) -> list[str]:
    head = [f"Current: {current}   (this quest)", ""] if current else []
    return [*head, "Available:", *(f"  {n}" for n in names), "", ROSTER_FOOT]


def current_provider_for(quest: Any) -> str | None:
    """The vendor this quest runs on. Reloads the pin first — another process
    may have written it since the client loaded the quest. State is read only
    where it exists: the pre-session opening prompt runs on a draft that has
    none."""
    from imp.engine.provider_pin import current_provider
    from imp.engine.state import State
    quest.reload_provider()
    state_dir = quest.state_dir
    state = (State.load(state_dir) if (state_dir / "state.json").exists()
             else None)
    return current_provider(quest, state)


def print_provider_roster(*, quest: Any,
                          output_fn: Callable[[str], None]) -> None:
    for line in render_provider_roster(current_provider_for(quest),
                                       selectable_available()):
        output_fn(line)
