from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from imp.cli.narrative import _elapsed_seconds


@dataclass
class StepRow:
    phase: str
    step: str
    start_ts: str | None
    end_ts: str | None = None
    wait_seconds: float = 0.0
    errored: bool = False

    @property
    def wall_seconds(self) -> float | None:
        """start → completion wall clock; None for an open (still-running) step."""
        return _elapsed_seconds(self.start_ts, self.end_ts)


def _is_nonterminal_error(p: dict[str, Any]) -> bool:
    # The executor's interrupt marker is not a terminal failure (narrative._h_error special-cases the same shape).
    return p.get("category") == "config" and p.get("kind") == "interrupted"


def build_summary(events: Iterable[dict[str, Any]]) -> list[StepRow]:
    rows: list[StepRow] = []
    open_row: StepRow | None = None        # the currently-open occurrence
    pending_wait_start: str | None = None  # ts of an unresolved awaiting_user_input

    def _close_wait(now_ts: str | None) -> None:
        nonlocal pending_wait_start
        if pending_wait_start is not None and open_row is not None:
            delta = _elapsed_seconds(pending_wait_start, now_ts)
            if delta is not None and delta > 0:
                open_row.wait_seconds += delta
        pending_wait_start = None

    for e in events:
        if not isinstance(e, dict):
            continue
        etype = e.get("type")
        ts = e.get("timestamp")
        raw = e.get("payload")
        p = raw if isinstance(raw, dict) else {}

        if etype == "step_started":
            step = str(p.get("step", "?"))
            phase = str(p.get("phase", "?"))
            _close_wait(ts)
            # Resume re-entry: same step still open in same phase (paused mid-flight, no step_completed).
            if (open_row is not None and open_row.end_ts is None
                    and open_row.step == step and open_row.phase == phase):
                continue
            open_row = StepRow(phase=phase, step=step, start_ts=ts)
            rows.append(open_row)
        elif etype == "step_completed":
            _close_wait(ts)
            if open_row is not None and open_row.end_ts is None:
                open_row.end_ts = ts
            open_row = None
        elif etype == "awaiting_user_input":
            pending_wait_start = ts
        elif etype == "error" and not _is_nonterminal_error(p):
            if open_row is not None and open_row.end_ts is None:
                open_row.errored = True
    return rows


def _fmt_compact(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60}m{total % 60:02d}s"
