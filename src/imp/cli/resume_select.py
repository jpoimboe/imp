from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generic, TypeVar

from imp.cli import change_row


@dataclass(frozen=True)
class Candidate:
    change_id: str
    title: str
    status: str
    step: str
    last_iso: str


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
        for i, c in enumerate(self.candidates):
            marker = "▸ " if i == self.index else "  "
            out.append(marker + change_row.format_change_line(
                change_id=c.change_id, title=c.title, status=c.status,
                step=c.step, last_iso=c.last_iso, now=self.now, color=self.color))
        return "\n".join(out)


async def run_picker(candidates: list[Candidate], *, now: datetime,
                     color: bool, header: str, selected_index: int = 0) -> str | None:
    """Draw the inline arrow-key picker; return the chosen change id, or None when
    the user cancels (q / esc / ctrl-c). Uses Application.run_async so it nests on
    the event loop imp's anyio.run already owns."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    from imp.cli import palette as pal

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
        event.app.exit(result=model.selected().change_id)

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
        layout=Layout(HSplit([head, body])), key_bindings=kb, full_screen=False)
    # _enter exits with a change_id (str); _cancel (q/esc/c-c) exits with None.
    result: str | None = await app.run_async()
    return result


T = TypeVar("T")


class _ItemPicker(Generic[T]):
    def __init__(self, items: list[T], render_item: Callable[[T], str],
                 *, color: bool) -> None:
        self.items = items
        self.render_item = render_item
        self.color = color
        self.index = 0

    def move_up(self) -> None:
        self.index = max(0, self.index - 1)

    def move_down(self) -> None:
        self.index = min(len(self.items) - 1, self.index + 1)

    def selected(self) -> T:
        return self.items[self.index]

    def render(self) -> str:
        from imp.cli import palette as pal
        out = []
        for i, item in enumerate(self.items):
            marker = "▸ " if i == self.index else "  "
            row = marker + self.render_item(item)
            if self.color and i != self.index:
                row = pal.ansi(row, "dim", enabled=True)
            out.append(row)
        return "\n".join(out)


async def run_item_picker(
    items: list[T], *, render_item: Callable[[T], str], header: str, color: bool,
) -> T | None:
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    from imp.cli import palette as pal

    model = _ItemPicker(items, render_item, color=color)
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
        event.app.exit(result=model.selected())

    @kb.add("q")
    @kb.add("escape")
    @kb.add("c-c")
    def _cancel(event: Any) -> None:
        event.app.exit(result=None)

    head = Window(
        content=FormattedTextControl(
            lambda: ANSI(pal.ansi(header, "dim", enabled=model.color))),
        height=1)
    body = Window(content=FormattedTextControl(lambda: ANSI(model.render())))
    app: Any = Application(
        layout=Layout(HSplit([head, body])), key_bindings=kb, full_screen=False)
    result: T | None = await app.run_async()
    return result
