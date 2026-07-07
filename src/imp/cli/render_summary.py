from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from imp.cli import palette as pal
from imp.cli.change_row import STATUS_COLORS
from imp.cli.narrative import TokenTotals, elapsed_seconds
from imp.cli.render_text import fmt_seconds


@dataclass
class SubagentRow:
    name: str
    label: str | None = None
    start_ts: str | None = None
    end_ts: str | None = None
    errored: bool = False
    tokens: TokenTotals = field(default_factory=TokenTotals)
    cost: float = 0.0

    @property
    def wall_seconds(self) -> float | None:
        """dispatch → result wall clock; None while the subagent is still running."""
        return elapsed_seconds(self.start_ts, self.end_ts)


@dataclass
class StepRow:
    phase: str
    step: str
    start_ts: str | None
    end_ts: str | None = None
    wait_seconds: float = 0.0
    errored: bool = False
    agent_id: str | None = None
    tokens: TokenTotals = field(default_factory=TokenTotals)
    cost: float = 0.0
    subagents: list[SubagentRow] = field(default_factory=list)

    @property
    def wall_seconds(self) -> float | None:
        """start → completion wall clock; None for an open (still-running) step."""
        return elapsed_seconds(self.start_ts, self.end_ts)

    @property
    def work_seconds(self) -> float | None:
        """wall clock minus accrued human wait; None while the step is still open."""
        wall = self.wall_seconds
        if wall is None:
            return None
        return max(0.0, wall - self.wait_seconds)


def _is_nonterminal_error(p: dict[str, Any]) -> bool:
    # Executor's interrupt marker is not a terminal failure; narrative._h_error same shape.
    return p.get("category") == "config" and p.get("kind") == "interrupted"


def build_summary(events: Iterable[dict[str, Any]]) -> list[StepRow]:
    rows: list[StepRow] = []
    open_row: StepRow | None = None        # the currently-open occurrence
    pending_wait_start: str | None = None  # ts of an unresolved awaiting_user_input

    def _close_wait(now_ts: str | None) -> None:
        nonlocal pending_wait_start
        if pending_wait_start is not None and open_row is not None:
            delta = elapsed_seconds(pending_wait_start, now_ts)
            if delta is not None and delta > 0:
                open_row.wait_seconds += delta
        pending_wait_start = None

    for e in events:
        etype = e.get("type")
        ts = e.get("timestamp")
        agent_id = e.get("agent_id")
        raw = e.get("payload")
        p = raw if isinstance(raw, dict) else {}

        # Map an open step to the agent id of its first non-sub agent event.
        if etype in ("step_context", "llm_response"):
            if (open_row is not None and open_row.end_ts is None
                    and open_row.agent_id is None
                    and isinstance(agent_id, str)
                    and not agent_id.startswith("sub:")):
                open_row.agent_id = agent_id

        if etype == "step_started":
            step = str(p.get("step", "?"))
            phase = str(p.get("phase", "?"))
            _close_wait(ts)
            # Resume re-entry: same step still open — fold into existing occurrence.
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
        elif etype == "llm_response":
            if (open_row is not None
                    and agent_id is not None and agent_id == open_row.agent_id):
                open_row.tokens.add_usage(p.get("usage"))
        elif etype == "query_result":
            if (open_row is not None
                    and agent_id is not None and agent_id == open_row.agent_id):
                open_row.cost += float(p.get("total_cost_usd") or 0)
        elif etype == "error" and not _is_nonterminal_error(p):
            if open_row is not None and open_row.end_ts is None:
                open_row.errored = True
    return rows


@dataclass
class SummaryHeader:
    workflow: str
    change_id: str
    status: str
    total_seconds: float | None


_HUE_ROTATION: tuple[str, ...] = ("step", "magenta", "cyan", "green")
_WAIT_THRESHOLD = 5.0


def summary_total(events: list[dict[str, Any]]) -> float | None:
    if not events:
        return None
    return elapsed_seconds(events[0].get("timestamp"), events[-1].get("timestamp"))


def _header_line(h: SummaryHeader, *, color: bool) -> str:
    workflow = pal.ansi(h.workflow, *pal.NODE_COLORS["workflow"], enabled=color)
    status = pal.ansi(h.status, *STATUS_COLORS.get(h.status, ()), enabled=color)
    total = "running" if h.total_seconds is None else fmt_seconds(h.total_seconds)
    return f"{workflow}  ·  {h.change_id}  ·  {status}  ·  {total}"


def _step_line(row: StepRow, *, hue: str, name_w: int, color: bool) -> str:
    padded = f"{row.step:<{name_w}}"
    codes = pal.NODE_COLORS["error"] if row.errored else (hue,)
    label = pal.ansi(padded, *codes, enabled=color)
    wall = row.wall_seconds
    dur_text = "running" if wall is None else fmt_seconds(wall)
    dur = pal.ansi(dur_text, "gray", enabled=color)
    line = f"  {label}  {dur}"
    if row.wait_seconds >= _WAIT_THRESHOLD:
        wait = pal.ansi(f"⏸ {fmt_seconds(row.wait_seconds)} wait",
                        "bright_blue", enabled=color)
        line += f"   {wait}"
    return line


def render_summary(summary: list[StepRow], *, header: SummaryHeader,
                   color: bool, width: int) -> list[str]:
    lines = [_header_line(header, color=color)]
    name_w = max((len(r.step) for r in summary), default=0)
    cur_phase: str | None = None
    hues: dict[str, str] = {}
    next_hue = 0
    for row in summary:
        if row.phase != cur_phase:
            cur_phase = row.phase
            hues, next_hue = {}, 0
            lines.append(pal.ansi(row.phase, *pal.NODE_COLORS["phase"], enabled=color))
        if row.step not in hues:
            hues[row.step] = _HUE_ROTATION[next_hue % len(_HUE_ROTATION)]
            next_hue += 1
        lines.append(_step_line(row, hue=hues[row.step], name_w=name_w, color=color))
    return lines
