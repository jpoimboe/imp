# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
from __future__ import annotations

from typing import Any

import anyio

from imp.agents.tools import imp_tool


@imp_tool(
    name="save_and_exit",
    description=(
        "Cleanly save this conversation and drop to the shell: a graceful pause "
        "at the current step — nothing built, no worktree — resumable later with "
        "`imp resume <id>`. Use when the user chooses to set the quest aside "
        "instead of building now."
    ),
)
async def save_and_exit(ctx: Any) -> str:
    if ctx.quest_state is None:
        raise RuntimeError(
            "save_and_exit is only available to top-level quest steps")
    if ctx.coordinator is None:
        raise RuntimeError(
            "save_and_exit requires an interactive session coordinator")
    return "Saved. The session closes at the end of this turn."


async def request_save_and_park(ctx: Any) -> None:
    ctx.coordinator.request_save()
    # CONTROL_SAVE cancels the run, which unwinds this park.
    await anyio.sleep_forever()
