# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Pinned copy for confirming the teardown of a built quest."""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from imp.engine import worktree

if TYPE_CHECKING:
    from imp.engine.quest import Quest


def removal_description(quest: Quest) -> str | None:
    """What abandoning `quest` removes; None for a draft, which has nothing built."""
    if not quest.materialized:
        return None
    n = worktree.commit_count(quest.control_root, quest.id, quest.base_branch)
    count = "no commits" if n == 0 else "1 commit" if n == 1 else f"{n} commits"
    return f"worktree and branch imp/{quest.id} ({count})"


def abandon_question(quest_id: str, removed: str) -> str:
    return f"Abandon {quest_id}? This removes its {removed}. [y/N] "


async def ask_keypress_confirm(question: str) -> bool:
    """Single-keypress y/N: y or Y confirms, any other key declines."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    kb = KeyBindings()

    @kb.add("y")
    @kb.add("Y")
    def _yes(event: Any) -> None:
        event.app.exit(result=True)

    @kb.add("<any>")
    def _no(event: Any) -> None:
        event.app.exit(result=False)

    control = FormattedTextControl([("", question), ("[SetCursorPosition]", ""), ("", " ")])
    app: Any = Application(
        layout=Layout(Window(control, wrap_lines=True)), key_bindings=kb,
        full_screen=False, erase_when_done=True)
    if sys.stdin.isatty():
        import termios
        # Drop typed-ahead bytes so a stale key can't answer.
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    confirmed: bool = await app.run_async(handle_sigint=False)
    print(question + ("y" if confirmed else "n"))
    return confirmed
