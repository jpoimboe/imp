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
    subs_by_agent: dict[str, SubagentRow] = {}   # "sub:<event id>" -> row (tokens/cost)
    subs_by_block: dict[str, SubagentRow] = {}    # tool_call block id -> row (close)

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
        if (etype in ("step_context", "llm_response")
                and open_row is not None and open_row.end_ts is None
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
            sub = subs_by_agent.get(agent_id) if isinstance(agent_id, str) else None
            if sub is not None:
                sub.tokens.add_usage(p.get("usage"))
            elif (open_row is not None
                  and agent_id is not None and agent_id == open_row.agent_id):
                open_row.tokens.add_usage(p.get("usage"))
        elif etype == "query_result":
            cost = float(p.get("total_cost_usd") or 0)
            sub = subs_by_agent.get(agent_id) if isinstance(agent_id, str) else None
            if sub is not None:
                sub.cost += cost
            elif (open_row is not None
                  and agent_id is not None and agent_id == open_row.agent_id):
                open_row.cost += cost
        elif etype == "tool_call" and p.get("name") == "sub_agent":
            if open_row is not None and open_row.end_ts is None:
                event_id = str(e.get("id", ""))
                block_id = str(p.get("id", ""))
                inp = p.get("input") or {}
                sub = SubagentRow(
                    name=str(inp.get("subagent_name", "?")),
                    label=p.get("label"),
                    start_ts=ts)
                open_row.subagents.append(sub)
                subs_by_agent[f"sub:{event_id}"] = sub
                if block_id:
                    subs_by_block[block_id] = sub
        elif etype == "tool_result":
            sub = subs_by_block.get(str(p.get("id", "")))
            if sub is not None:
                sub.end_ts = ts
                sub.errored = bool(p.get("error", False))
        elif etype == "error" and not _is_nonterminal_error(p):
            if open_row is not None and open_row.end_ts is None:
                open_row.errored = True
    return rows


def _dur_text(seconds: float | None, open_label: str = "running") -> str:
    """Duration text via the shared fmt_seconds; open_label for an open span."""
    return open_label if seconds is None else fmt_seconds(seconds)


def _tok_total(t: TokenTotals) -> int:
    """A row's single token figure: input + output + both cache buckets."""
    return t.input + t.output + t.cache_read + t.cache_creation


def _fmt_tokens(total: int) -> str:
    """Human-formatted token count: '—' for 0, bare int below 1k, then k / M."""
    if total <= 0:
        return "—"
    if total < 1000:
        return str(total)
    if total < 999_950:  # 999_950+ rounds up to "1000.0k"; roll it into the M bucket instead.
        return f"{total / 1000:.1f}k"
    return f"{total / 1_000_000:.1f}M"


def _fmt_cost(cost: float) -> str:
    """Dollar cost: '—' for none, '<$0.01' for a sub-cent figure, else $X.XX."""
    if cost <= 0:
        return "—"
    if cost < 0.01:
        return "<$0.01"
    return f"${cost:.2f}"


def _sub_body(sub: SubagentRow) -> str:
    """The subagent's left-column label: '↳ name' plus ' (label)' when present."""
    text = f"↳ {sub.name}"
    if sub.label:
        text += f" ({sub.label})"
    return text


def header_totals(
    summary: list[StepRow], total_seconds: float | None
) -> tuple[float | None, int, float]:
    """Header rollup: work time (raw wall minus every step's human wait, floored
    at 0; None when the run has no total yet) plus tokens and cost summed over
    every step's own usage AND every subagent's own usage."""
    if total_seconds is None:
        work: float | None = None
    else:
        work = max(0.0, total_seconds - sum(r.wait_seconds for r in summary))
    tokens = sum(_tok_total(r.tokens) for r in summary)
    tokens += sum(_tok_total(s.tokens) for r in summary for s in r.subagents)
    cost = sum((r.cost for r in summary), 0.0)
    cost += sum(s.cost for r in summary for s in r.subagents)
    return work, tokens, cost


@dataclass
class SummaryHeader:
    workflow: str
    change_id: str
    status: str
    total_seconds: float | None


_HUE_ROTATION: tuple[str, ...] = ("step", "magenta", "cyan", "green")


def summary_total(events: list[dict[str, Any]]) -> float | None:
    if not events:
        return None
    return elapsed_seconds(events[0].get("timestamp"), events[-1].get("timestamp"))


def _header_line(h: SummaryHeader, work_seconds: float | None,
                 tokens: int, cost: float, *, color: bool) -> str:
    workflow = pal.ansi(h.workflow, *pal.NODE_COLORS["workflow"], enabled=color)
    status = pal.ansi(h.status, *STATUS_COLORS.get(h.status, ()), enabled=color)
    total = _dur_text(work_seconds)
    tok = _fmt_tokens(tokens)
    cst = _fmt_cost(cost)
    return (f"{workflow}  ·  {h.change_id}  ·  {status}"
            f"  ·  {total}  ·  {tok}  ·  {cst}")


def _column_widths(summary: list[StepRow], open_label: str) -> tuple[int, int, int, int]:
    """Column widths measured across every step AND subagent so numbers align."""
    names = [len(r.step) for r in summary]
    names += [len(_sub_body(s)) + 2 for r in summary for s in r.subagents]
    durs: list[int] = []
    toks: list[int] = []
    costs: list[int] = []
    for r in summary:
        durs.append(len(_dur_text(r.work_seconds, open_label)))
        toks.append(len(_fmt_tokens(_tok_total(r.tokens))))
        costs.append(len(_fmt_cost(r.cost)))
        for s in r.subagents:
            durs.append(len(_dur_text(s.wall_seconds)))
            toks.append(len(_fmt_tokens(_tok_total(s.tokens))))
            costs.append(len(_fmt_cost(s.cost)))
    return (max(names, default=0), max(durs, default=0),
            max(toks, default=0), max(costs, default=0))


def _step_line(row: StepRow, *, hue: str, name_w: int, dur_w: int,
               tok_w: int, cost_w: int, color: bool, open_label: str) -> str:
    codes = pal.NODE_COLORS["error"] if row.errored else (hue,)
    name = pal.ansi(f"{row.step:<{name_w}}", *codes, enabled=color)
    dur = pal.ansi(f"{_dur_text(row.work_seconds, open_label):>{dur_w}}", *codes, enabled=color)
    tok = f"{_fmt_tokens(_tok_total(row.tokens)):>{tok_w}}"
    cost = f"{_fmt_cost(row.cost):>{cost_w}}"
    return f"  {name}  {dur}  {tok}  {cost}"


def _sub_line(sub: SubagentRow, *, name_w: int, dur_w: int,
              tok_w: int, cost_w: int, color: bool) -> str:
    codes = pal.NODE_COLORS["error"] if sub.errored else pal.NODE_COLORS["subagent"]
    # The extra two-space indent is absorbed by narrowing the label field, so the
    # numeric columns stay aligned with the step rows (see _column_widths).
    body = pal.ansi(f"{_sub_body(sub):<{name_w - 2}}", *codes, enabled=color)
    dur = pal.ansi(f"{_dur_text(sub.wall_seconds):>{dur_w}}", *codes, enabled=color)
    tok = f"{_fmt_tokens(_tok_total(sub.tokens)):>{tok_w}}"
    cost = f"{_fmt_cost(sub.cost):>{cost_w}}"
    return f"    {body}  {dur}  {tok}  {cost}"


def render_summary(summary: list[StepRow], *, header: SummaryHeader,
                   color: bool, width: int) -> list[str]:
    open_label = header.status
    name_w, dur_w, tok_w, cost_w = _column_widths(summary, open_label)
    work, tokens, cost = header_totals(summary, header.total_seconds)
    lines = [_header_line(header, work, tokens, cost, color=color)]
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
        lines.append(_step_line(row, hue=hues[row.step], name_w=name_w,
                                dur_w=dur_w, tok_w=tok_w, cost_w=cost_w, color=color,
                                open_label=open_label))
        for sub in row.subagents:
            lines.append(_sub_line(sub, name_w=name_w, dur_w=dur_w,
                                   tok_w=tok_w, cost_w=cost_w, color=color))
    return lines
