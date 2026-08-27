# SPDX-License-Identifier: Apache-2.0
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
    coord = ctx.coordinator
    if coord is None:
        raise RuntimeError(
            "save_and_exit requires an interactive session coordinator")
    coord.request_save()
    # CONTROL_SAVE cancels the serve scope, which unwinds this parked task; the
    # daemon then runs save_as_draft post-lock. Parking is the only exit — the
    # tool never returns to the agent.
    await anyio.sleep_forever()
    raise AssertionError("unreachable")  # anyio 4.8: sleep_forever is -> None
