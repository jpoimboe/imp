# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Current-and-roster derivation for the provider pin, shared by the `p` picker
and both `/provider` worlds so no two arming surfaces can disagree about what is
current or what may be picked."""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from imp.providers import ProviderGroups

PICKER_HEAD = (
    "switch provider — the rest of this quest runs on your pick",
    "takes effect right away — this step is waiting to retry",
)
PICKER_FOOT = "press a number, or esc to cancel"
ROSTER_FOOT = "Switch with /provider <name> — the rest of this quest runs on it."


def quest_provider_groups(quest: Any) -> ProviderGroups:
    from imp.providers import provider_groups
    from imp.providers.effective import load_effective_config
    return provider_groups(
        load_effective_config(control_root=quest.control_root).config)


def render_picker(current: str | None, groups: ProviderGroups) -> list[str]:
    lines = [*PICKER_HEAD, ""]
    slot = 0
    for header, names in (("configured", groups.configured),
                          ("also installed", groups.also_installed)):
        if names:
            lines.append(header)
        for name in names:
            slot += 1
            lines.append(f"  {slot}  {name}   current" if name == current
                         else f"  {slot}  {name}")
    return [*lines, "", PICKER_FOOT]


def picker_choices(current: str | None,
                   groups: ProviderGroups) -> list[str | None]:
    """What each digit dispatches, in render_picker's order, the current
    vendor's slot None: a re-pick has no name to send, so it cannot arm a switch
    no reconcile would clear."""
    return [None if n == current else n
            for n in (*groups.configured, *groups.also_installed)]


def render_provider_roster(current: str | None,
                           groups: ProviderGroups) -> list[str]:
    head = [f"Current: {current} (this quest)", ""] if current else []
    body: list[str] = []
    for header, names in (("Configured:", groups.configured),
                          ("Also installed:", groups.also_installed)):
        if names:
            body += [header, *(f"  {n}" for n in names)]
    return [*head, *body, "", ROSTER_FOOT]


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
                                       quest_provider_groups(quest)):
        output_fn(line)
