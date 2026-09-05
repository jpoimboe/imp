# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from imp.cli import palette as pal
from imp.cli.accounting import (
    RowUsage,
    billing_from_agent_message,
    billing_from_usage_patch,
    collapse_cumulative_query_results,
    computed_contribution,
    is_metered,
    vendor_contribution,
)
from imp.cli.narrative import elapsed_seconds
from imp.cli.quest_row import STATUS_COLORS, display_word
from imp.cli.render_text import fmt_seconds

_FINISHED_STATUSES = frozenset({"merged", "done", "abandoned"})


def open_span_label(status: str) -> str:
    """A finished quest's open span was interrupted; an unfinished one's is still in that status."""
    return "interrupted" if status in _FINISHED_STATUSES else display_word(status)


def _dur_text(seconds: float | None, open_label: str = "running", *,
              inferred: bool = False) -> str:
    """`~` marks an inferred end; open_label names a span with no end at all."""
    if seconds is None:
        return open_label
    return f"~{fmt_seconds(seconds)}" if inferred else fmt_seconds(seconds)


@dataclass
class SubagentRow:
    name: str
    label: str | None = None
    start_ts: str | None = None
    end_ts: str | None = None
    end_inferred: bool = False
    errored: bool = False
    usage: RowUsage = field(default_factory=RowUsage)

    @property
    def wall_seconds(self) -> float | None:
        """dispatch → result wall clock; None while the subagent is still running."""
        return elapsed_seconds(self.start_ts, self.end_ts)

    def dur_cell(self, open_label: str) -> str:
        return _dur_text(self.wall_seconds, open_label, inferred=self.end_inferred)


@dataclass
class StepRow:
    phase: str
    step: str
    start_ts: str | None
    end_ts: str | None = None
    end_inferred: bool = False
    wait_seconds: float = 0.0
    errored: bool = False
    agent_id: str | None = None
    usage: RowUsage = field(default_factory=RowUsage)
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

    def dur_cell(self, open_label: str) -> str:
        return _dur_text(self.work_seconds, open_label, inferred=self.end_inferred)


def _is_nonterminal_error(p: dict[str, Any]) -> bool:
    # Audit-only records are queryable but silent; interrupt marker is not a terminal failure.
    if p.get("audit"):
        return True
    return p.get("category") == "config" and p.get("kind") == "interrupted"


def build_summary(events: Iterable[dict[str, Any]]) -> list[StepRow]:
    rows: list[StepRow] = []
    open_row: StepRow | None = None        # the currently-open occurrence
    pending_wait_start: str | None = None  # ts of an unresolved awaiting_user_input
    subs_by_agent: dict[str, SubagentRow] = {}   # "sub:<event id>" -> row (usage)
    # (call was id_ns-stamped, block id) -> row. Keying on the stamping is what stops an
    # unstamped result from closing a stamped call: codex block ids repeat every turn.
    open_subs: dict[tuple[bool, str], SubagentRow] = {}
    # provider/model/auth_mode of the last agent_message per agent: the patch
    # and query_result rows carry none of the three and inherit all of them.
    last_msg: dict[str, tuple[str | None, str | None, str | None]] = {}
    agent_id: Any = None  # function-scope so the _credit closure reads the current event's id

    def _close_wait(now_ts: str | None) -> None:
        nonlocal pending_wait_start
        if pending_wait_start is not None and open_row is not None:
            delta = elapsed_seconds(pending_wait_start, now_ts)
            if delta is not None and delta > 0:
                open_row.wait_seconds += delta
        pending_wait_start = None

    def _contain_subs(row: StepRow | None, end_ts: str | None) -> None:
        """Close off the step's list, not the table: a reused block id evicts its predecessor."""
        if row is not None and end_ts is not None:
            for sub in row.subagents:
                if sub.end_ts is None:
                    sub.end_ts = end_ts
                    sub.end_inferred = True
        open_subs.clear()
        subs_by_agent.clear()

    def _credit(usage: RowUsage) -> None:
        sub = subs_by_agent.get(agent_id) if isinstance(agent_id, str) else None
        if sub is not None:
            sub.usage = sub.usage + usage
        elif (open_row is not None
              and agent_id is not None and agent_id == open_row.agent_id):
            open_row.usage = open_row.usage + usage

    for e in collapse_cumulative_query_results(list(events)):
        etype = e.get("type")
        ts = e.get("timestamp")
        agent_id = e.get("agent_id")
        raw = e.get("payload")
        p = raw if isinstance(raw, dict) else {}

        # Map an open step to the agent id of its first non-sub agent event.
        if (etype in ("step_context", "agent_message")
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
            if open_row is not None and open_row.end_ts is None:
                _contain_subs(open_row, ts)
                if ts is not None:
                    open_row.end_ts = ts
                    open_row.end_inferred = True
            open_row = StepRow(phase=phase, step=step, start_ts=ts)
            rows.append(open_row)
        elif etype == "step_completed":
            _close_wait(ts)
            if open_row is not None and open_row.end_ts is None:
                open_row.end_ts = ts
            _contain_subs(open_row, ts)
            open_row = None
        elif etype == "awaiting_user_input":
            pending_wait_start = ts
        elif etype == "agent_message":
            if isinstance(agent_id, str):
                last_msg[agent_id] = (p.get("provider"), p.get("model"),
                                      p.get("auth_mode"))
            # claude_code's agent_message usage is the unreliable start-of-message
            # snapshot; its authoritative figures arrive on query_result.
            billing = (None if p.get("provider") == "claude_code"
                       else billing_from_agent_message(p))
            if billing is not None:
                _credit(computed_contribution(
                    p.get("model"), billing,
                    billing.fresh_input + billing.cached_input + billing.cache_write,
                    metered=is_metered(p.get("provider"), p.get("auth_mode"))))
        elif etype == "agent_message_usage_patch":
            billing = billing_from_usage_patch(p)
            if billing is not None:
                provider, model, auth_mode = last_msg.get(
                    agent_id if isinstance(agent_id, str) else "",
                    (None, None, None))
                # context_tokens is codex's own occupancy reading; absent means
                # no reading, and absent evidence is never a 2x charge.
                _credit(computed_contribution(
                    model, billing, p.get("context_tokens"),
                    metered=is_metered(provider, auth_mode)))
        elif etype == "query_result":
            provider, _model, auth_mode = last_msg.get(
                agent_id if isinstance(agent_id, str) else "", (None, None, None))
            # The vendor's own cost, never the price table: it already folds in
            # web search and covers models the table does not carry.
            vendor = vendor_contribution(
                p.get("model_usage") or {}, p.get("total_cost_usd"),
                metered=is_metered(provider, auth_mode))
            if vendor is not None:
                _credit(vendor)
        elif etype == "tool_call" and p.get("name") == "sub_agent":
            if open_row is not None and open_row.end_ts is None:
                event_id = str(e.get("id", ""))
                block_id = str(p.get("id", ""))
                inp = p.get("input") or {}
                new_sub = SubagentRow(
                    name=str(inp.get("subagent_name", "?")),
                    label=p.get("label"),
                    start_ts=ts)
                open_row.subagents.append(new_sub)
                subs_by_agent[f"sub:{event_id}"] = new_sub
                if block_id:
                    open_subs[(p.get("id_ns") == "block", block_id)] = new_sub
        elif etype == "tool_result":
            id_ns = p.get("id_ns")
            if id_ns is None or id_ns == "block":
                sub = open_subs.pop((id_ns == "block", str(p.get("id", ""))), None)
                if sub is not None:
                    sub.end_ts = ts
                    sub.errored = bool(p.get("error", False))
        elif etype == "error" and not _is_nonterminal_error(p):
            if open_row is not None and open_row.end_ts is None:
                open_row.errored = True
    return rows


def _tok_total(u: RowUsage) -> int:
    """A row's single token figure: the whole input side plus output."""
    return u.input_total + u.output


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
    tokens = sum(_tok_total(r.usage) for r in summary)
    tokens += sum(_tok_total(s.usage) for r in summary for s in r.subagents)
    cost = sum((r.usage.dollars for r in summary), 0.0)
    cost += sum(s.usage.dollars for r in summary for s in r.subagents)
    return work, tokens, cost


@dataclass
class SummaryHeader:
    workflow: str
    quest_id: str
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
    status = pal.ansi(display_word(h.status), *STATUS_COLORS.get(h.status, ()), enabled=color)
    total = _dur_text(work_seconds)
    tok = _fmt_tokens(tokens)
    cst = _fmt_cost(cost)
    return (f"{workflow}  ·  {h.quest_id}  ·  {status}"
            f"  ·  {total}  ·  {tok}  ·  {cst}")


def _column_widths(summary: list[StepRow], open_label: str) -> tuple[int, int, int, int]:
    """Column widths measured across every step AND subagent so numbers align."""
    names = [len(r.step) for r in summary]
    names += [len(_sub_body(s)) + 2 for r in summary for s in r.subagents]
    durs: list[int] = []
    toks: list[int] = []
    costs: list[int] = []
    for r in summary:
        durs.append(len(r.dur_cell(open_label)))
        toks.append(len(_fmt_tokens(_tok_total(r.usage))))
        costs.append(len(_fmt_cost(r.usage.dollars)))
        for s in r.subagents:
            durs.append(len(s.dur_cell(open_label)))
            toks.append(len(_fmt_tokens(_tok_total(s.usage))))
            costs.append(len(_fmt_cost(s.usage.dollars)))
    return (max(names, default=0), max(durs, default=0),
            max(toks, default=0), max(costs, default=0))


def _step_line(row: StepRow, *, hue: str, name_w: int, dur_w: int,
               tok_w: int, cost_w: int, color: bool, open_label: str) -> str:
    codes = pal.NODE_COLORS["error"] if row.errored else (hue,)
    name = pal.ansi(f"{row.step:<{name_w}}", *codes, enabled=color)
    dur = pal.ansi(f"{row.dur_cell(open_label):>{dur_w}}", *codes, enabled=color)
    tok = f"{_fmt_tokens(_tok_total(row.usage)):>{tok_w}}"
    cost = f"{_fmt_cost(row.usage.dollars):>{cost_w}}"
    return f"  {name}  {dur}  {tok}  {cost}"


def _sub_line(sub: SubagentRow, *, name_w: int, dur_w: int,
              tok_w: int, cost_w: int, color: bool, open_label: str) -> str:
    codes = pal.NODE_COLORS["error"] if sub.errored else pal.NODE_COLORS["subagent"]
    # The extra two-space indent is absorbed by narrowing the label field, so the
    # numeric columns stay aligned with the step rows (see _column_widths).
    body = pal.ansi(f"{_sub_body(sub):<{name_w - 2}}", *codes, enabled=color)
    dur = pal.ansi(f"{sub.dur_cell(open_label):>{dur_w}}", *codes, enabled=color)
    tok = f"{_fmt_tokens(_tok_total(sub.usage)):>{tok_w}}"
    cost = f"{_fmt_cost(sub.usage.dollars):>{cost_w}}"
    return f"    {body}  {dur}  {tok}  {cost}"


def render_summary(summary: list[StepRow], *, header: SummaryHeader,
                   color: bool, width: int) -> list[str]:
    open_label = open_span_label(header.status)
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
                                   tok_w=tok_w, cost_w=cost_w, color=color,
                                   open_label=open_label))
    return lines
