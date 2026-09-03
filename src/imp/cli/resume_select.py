# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from imp.cli import palette as pal
from imp.cli import quest_row


@dataclass(frozen=True)
class Candidate:
    quest_id: str
    title: str
    status: str
    step: str
    last_iso: str
    step_label: str | None = None
    phase_label: str | None = None
    intent: str | None = None


class ResumePicker:
    """Pure selection state. `candidates` arrive newest-first; `selected_index`
    chooses which row is pre-highlighted (0 = newest). `header` is the full
    caller-supplied prompt line drawn above the rows."""

    def __init__(self, candidates: list[Candidate], *, now: datetime,
                 color: bool, header: str, selected_index: int = 0) -> None:
        self.candidates = candidates
        self.now = now
        self.color = color
        self.header = header
        self.index = selected_index

    def move_up(self) -> None:
        self.index = max(0, self.index - 1)

    def move_down(self) -> None:
        self.index = min(len(self.candidates) - 1, self.index + 1)

    def selected(self) -> Candidate:
        return self.candidates[self.index]

    def render(self) -> str:
        out = []
        name_w = quest_row.name_budget(indent=2)  # the 2-char selection marker
        for i, c in enumerate(self.candidates):
            if i == self.index:
                # Inner line built color-off so no inner reset breaks the reverse bar.
                inner = "▸ " + quest_row.format_quest_line(
                    quest_id=c.quest_id, title=c.title,
                    status=c.status, step=c.step, step_label=c.step_label,
                    phase_label=c.phase_label, last_iso=c.last_iso, intent=c.intent,
                    now=self.now, color=False, name_width=name_w)
                out.append(pal.ansi(inner, "reverse", enabled=self.color))
            else:
                out.append("  " + quest_row.format_quest_line(
                    quest_id=c.quest_id, title=c.title,
                    status=c.status, step=c.step, step_label=c.step_label,
                    phase_label=c.phase_label, last_iso=c.last_iso, intent=c.intent,
                    now=self.now, color=self.color, name_width=name_w))
        return "\n".join(out)


async def run_picker(candidates: list[Candidate], *, now: datetime,
                     color: bool, header: str, selected_index: int = 0) -> str | None:
    """Draw the inline arrow-key picker; return the chosen quest id, or None when
    the user cancels (q / esc / ctrl-c). Uses Application.run_async so it nests on
    the event loop imp's anyio.run already owns."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    model = ResumePicker(candidates, now=now, color=color, header=header,
                         selected_index=selected_index)
    kb = KeyBindings()

    @kb.add("up")
    @kb.add("c-p")
    def _up(event: Any) -> None:
        model.move_up()

    @kb.add("down")
    @kb.add("c-n")
    def _down(event: Any) -> None:
        model.move_down()

    @kb.add("enter")
    def _enter(event: Any) -> None:
        event.app.exit(result=model.selected().quest_id)

    @kb.add("q")
    @kb.add("escape")
    @kb.add("c-c")
    def _cancel(event: Any) -> None:
        event.app.exit(result=None)

    head = Window(
        content=FormattedTextControl(lambda: ANSI(pal.ansi(
            model.header, "dim", enabled=color))),
        height=1)
    body = Window(content=FormattedTextControl(lambda: ANSI(model.render())))
    app: Any = Application(
        layout=Layout(HSplit([head, body])), key_bindings=kb, full_screen=False,
        erase_when_done=True)
    # _enter exits with a quest_id (str); _cancel (q/esc/c-c) exits with None.
    result: str | None = await app.run_async()
    return result

