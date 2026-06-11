from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from imp.cli import change_row


@dataclass(frozen=True)
class Candidate:
    change_id: str
    title: str
    status: str
    step: str
    last_iso: str


class ResumePicker:
    """Pure selection state. `candidates` arrive newest-first, so index 0 (the
    default) pre-selects the newest Change."""

    def __init__(self, candidates: list[Candidate], *, now: datetime,
                 color: bool) -> None:
        self.candidates = candidates
        self.now = now
        self.color = color
        self.index = 0

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
