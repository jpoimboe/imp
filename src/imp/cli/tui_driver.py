from __future__ import annotations

from collections.abc import Callable
from typing import Any

from imp.cli import narrative as nv
from imp.cli import render_text as rt
from imp.cli.live_driver import compose_activity_status


class TuiDriver:
    def __init__(
        self, *, color: bool, width: int, display_width: int = 80,
        on_invalidate: Callable[[], None] | None = None,
    ) -> None:
        self._builder = nv.NarrativeBuilder()
        self._color = color
        self._width = width
        self._display_width = display_width
        self._on_invalidate = on_invalidate

    def on_event(self, event: Any) -> None:
        self._builder.add(event)
        if self._on_invalidate is not None:
            self._on_invalidate()

    def finalize(self) -> None:
        """Run-end flush (mirrors LiveDriver.finish); idempotent."""
        self._builder.finalize()

    def seed_opening_intent(self, text: str) -> None:
        self._builder.seed_opening_intent(text)
        if self._on_invalidate is not None:
            self._on_invalidate()

    def seed_pending_reply(self, text: str) -> None:
        self._builder.seed_pending_reply(text)
        if self._on_invalidate is not None:
            self._on_invalidate()

    def render(self, *, full: bool) -> str:
        return "\n".join(rt.render_tree(
            self._builder.tree, full=full, color=self._color, width=self._width,
            display_width=self._display_width, live_viewer=True))

    def status_text(self, frame: int, elapsed: float) -> str | None:
        return compose_activity_status(
            self._builder, frame=frame, elapsed=elapsed, color=self._color,
            width=self._width)

    def print_collapsed_transcript(self, stream: Any) -> None:
        for line in rt.render_tree(
                self._builder.tree, full=False, color=self._color,
                width=self._width, display_width=self._display_width, live_viewer=True):
            stream.write(line + "\n")
        stream.flush()
