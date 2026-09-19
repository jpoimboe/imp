# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from imp.cli import palette as pal
from imp.cli.accounting import (
    RowUsage,
    billing_from_agent_message,
    billing_from_usage_patch,
    computed_contribution,
    is_metered,
    kept_query_results,
    vendor_contribution,
)
from imp.cli.narrative import elapsed_seconds
from imp.cli.quest_row import STATUS_COLORS, display_word
from imp.cli.quota_column import FOOTNOTE, QuotaColumn, QuotaFallback
from imp.cli.quota_share import NO_UNITS, Units, credits_by_index
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


def _union_seconds(segments: list[tuple[float, float]]) -> float:
    """Total length of the union of half-open segments: overlaps counted once."""
    total = 0.0
    mark = float("-inf")
    for lo, hi in sorted(segments):
        lo = max(lo, mark)
        if hi > lo:
            total += hi - lo
        mark = max(mark, hi)
    return total


@dataclass(frozen=True)
class Exclusions:
    """Wall-clock intervals of a run that are not work: human waits and pauses."""

    spans: tuple[tuple[str, str], ...] = ()

    def overlap(self, start_ts: str | None, end_ts: str | None) -> float:
        """Union seconds falling inside [start_ts, end_ts]; 0.0 for an open span."""
        width = elapsed_seconds(start_ts, end_ts)
        if width is None or width <= 0:
            return 0.0
        clipped: list[tuple[float, float]] = []
        for open_ts, close_ts in self.spans:
            lo = elapsed_seconds(start_ts, open_ts)
            hi = elapsed_seconds(start_ts, close_ts)
            if lo is None or hi is None:
                continue
            lo, hi = max(0.0, lo), min(width, hi)
            if hi > lo:
                clipped.append((lo, hi))
        return _union_seconds(clipped)

    @property
    def total(self) -> float:
        """Union seconds over the whole run."""
        if not self.spans:
            return 0.0
        ref = self.spans[0][0]  # spans aren't in start order, so offsets are deliberately signed
        segments: list[tuple[float, float]] = []
        for open_ts, close_ts in self.spans:
            lo = elapsed_seconds(ref, open_ts)
            hi = elapsed_seconds(ref, close_ts)
            if lo is not None and hi is not None and hi > lo:
                segments.append((lo, hi))
        return _union_seconds(segments)


NO_EXCLUSIONS = Exclusions()


def exclusion_spans(events: Iterable[dict[str, Any]]) -> Exclusions:
    """Human waits (`awaiting_user_input` → the reply) and pauses (`workflow_paused`
    → `workflow_resumed`), each also closed by the step boundary that ends it."""
    spans: list[tuple[str, str]] = []
    wait_start: str | None = None
    pause_start: str | None = None
    for e in events:
        etype = e.get("type")
        ts = e.get("timestamp")
        if not isinstance(ts, str):
            continue
        if etype == "awaiting_user_input":
            if wait_start is None:  # a re-armed gate keeps the first ask
                wait_start = ts
        elif etype == "workflow_paused":
            if pause_start is None:
                pause_start = ts
        elif etype == "user_message":
            if wait_start is not None:
                spans.append((wait_start, ts))
                wait_start = None
        elif etype == "workflow_resumed":
            if pause_start is not None:
                spans.append((pause_start, ts))
                pause_start = None
        elif etype in ("step_started", "step_completed"):
            if wait_start is not None:
                spans.append((wait_start, ts))
                wait_start = None
            if pause_start is not None:
                spans.append((pause_start, ts))
                pause_start = None
    return Exclusions(tuple(spans))


@dataclass
class SubagentRow:
    name: str
    label: str | None = None
    start_ts: str | None = None
    end_ts: str | None = None
    end_inferred: bool = False
    errored: bool = False
    usage: RowUsage = field(default_factory=RowUsage)
    units: Units = NO_UNITS
    agent_id: str = ""

    @property
    def key(self) -> str:
        """Row identity across a filtered and an unfiltered build: one dispatch."""
        return self.agent_id

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
    wait_seconds: float = 0.0  # exclusion union clipped to this row's span
    errored: bool = False
    agent_id: str | None = None
    usage: RowUsage = field(default_factory=RowUsage)
    subagents: list[SubagentRow] = field(default_factory=list)
    units: Units = NO_UNITS

    @property
    def key(self) -> str:
        """Row identity across a filtered and an unfiltered build: a step agent id
        repeats on re-run, so the occurrence's own start is what tells them apart."""
        return f"step:{self.phase}/{self.step}@{self.start_ts}"

    @property
    def totals(self) -> RowUsage:
        """Whole subtree: own usage plus every subagent's, matching the inclusive `time` column."""
        total = self.usage
        for sub in self.subagents:
            total = total + sub.usage
        return total

    @property
    def subtree_units(self) -> Units:
        """Whole subtree, on the same inclusive basis as `totals`."""
        total = self.units
        for sub in self.subagents:
            total = total + sub.units
        return total

    @property
    def wall_seconds(self) -> float | None:
        """start → completion wall clock; None for an open (still-running) step."""
        return elapsed_seconds(self.start_ts, self.end_ts)

    @property
    def work_seconds(self) -> float | None:
        """wall clock minus the row's exclusions; None while the step is still open."""
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


def build_summary(events: Iterable[dict[str, Any]],
                  units_span: tuple[datetime, datetime] | None = None,
                  ) -> list[StepRow]:
    """The summary's step rows. `units_span` turns on quota attribution, always
    over the events passed here: pass the unfiltered log and `overlay_units` the
    result onto a filtered build, so no filter can move a step's quota cell."""
    log = list(events)
    credits = credits_by_index(log, units_span) if units_span is not None else {}
    kept = kept_query_results(log)
    exclusions = exclusion_spans(log)
    rows: list[StepRow] = []
    open_row: StepRow | None = None        # the currently-open occurrence
    subs_by_agent: dict[str, SubagentRow] = {}   # "sub:<event id>" -> row (usage)
    # (call was id_ns-stamped, block id) -> row. Keying on the stamping is what stops an
    # unstamped result from closing a stamped call: codex block ids repeat every turn.
    open_subs: dict[tuple[bool, str], SubagentRow] = {}
    # provider/model/auth_mode of the last agent_message per agent: the patch
    # and query_result rows carry none of the three and inherit all of them.
    last_msg: dict[str, tuple[str | None, str | None, str | None]] = {}
    agent_id: Any = None  # function-scope so the _credit closure reads the current event's id

    def _contain_subs(row: StepRow | None, end_ts: str | None) -> None:
        """Close off the step's list, not the table: a reused block id evicts its predecessor."""
        if row is not None and end_ts is not None:
            for sub in row.subagents:
                if sub.end_ts is None:
                    sub.end_ts = end_ts
                    sub.end_inferred = True
        open_subs.clear()
        subs_by_agent.clear()

    def _target() -> StepRow | SubagentRow | None:
        sub = subs_by_agent.get(agent_id) if isinstance(agent_id, str) else None
        if sub is not None:
            return sub
        if (open_row is not None
                and agent_id is not None and agent_id == open_row.agent_id):
            return open_row
        return None

    def _credit(usage: RowUsage) -> None:
        row = _target()
        if row is not None:
            row.usage = row.usage + usage

    def _credit_units(index: int) -> None:
        credited = credits.get(index)
        if credited is None:
            return
        row = _target()
        if row is not None:
            row.units = row.units + credited

    for i, e in enumerate(log):
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

        _credit_units(i)
        if etype == "query_result" and i not in kept:
            continue  # cumulative predecessor: its dollars ride the surviving row

        if etype == "step_started":
            step = str(p.get("step", "?"))
            phase = str(p.get("phase", "?"))
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
            if open_row is not None and open_row.end_ts is None:
                open_row.end_ts = ts
            _contain_subs(open_row, ts)
            open_row = None
        elif etype == "agent_message":
            if isinstance(agent_id, str):
                last_msg[agent_id] = (p.get("provider"), p.get("model"),
                                      p.get("auth_mode"))
            # claude's agent_message usage is the unreliable start-of-message
            # snapshot; its authoritative figures arrive on query_result.
            billing = (None if p.get("provider") == "claude"
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
            # The vendor's own cost when it carries one — it already folds in
            # web search and covers models the table does not; else the
            # model_usage rollup, priced per model from the table.
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
                    start_ts=ts,
                    agent_id=f"sub:{event_id}")
                open_row.subagents.append(new_sub)
                subs_by_agent[new_sub.agent_id] = new_sub
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
    for row in rows:
        row.wait_seconds = exclusions.overlap(row.start_ts, row.end_ts)
    return rows


def _fmt_tokens(total: int) -> str:
    """Human-formatted token count: '—' for 0, bare int below 1k, then k / M."""
    if total <= 0:
        return "—"
    if total < 1000:
        return str(total)
    if total < 999_950:  # 999_950+ rounds up to "1000.0k"; roll it into the M bucket instead.
        return f"{total / 1000:.1f}k"
    return f"{total / 1_000_000:.1f}M"


LEGEND_METERED = "~ estimated at API list price"
LEGEND_NOTIONAL = "* theoretical API list price; actual subscription usage is much lower"
LEGEND_FLOOR = "≥ excludes steps run on a model absent from the price table"


def cost_cell(u: RowUsage) -> str:
    """The cost figure for any row, leaf or aggregate; the counters are the state."""
    if u.priced + u.unpriced == 0:
        return "—"
    if u.priced == 0:
        return "n/a"
    body = "<$0.01" if u.dollars < 0.01 else f"${u.dollars:.2f}"
    marked = f"{body}*" if u.notional else f"~{body}"
    return f"≥{marked}" if u.unpriced else marked


def legend_lines(cells: list[str]) -> list[str]:
    """The legend for exactly the cells rendered, tilde first."""
    bodies = [c.removeprefix("≥") for c in cells]
    lines: list[str] = []
    if any(b.startswith("~") for b in bodies):
        lines.append(LEGEND_METERED)
    if any(b.endswith("*") for b in bodies):
        lines.append(LEGEND_NOTIONAL)
    if any(c.startswith("≥") for c in cells):
        lines.append(LEGEND_FLOOR)
    return lines


def _sub_body(sub: SubagentRow) -> str:
    """The subagent's left-column label: '↳ name' plus ' (label)' when present."""
    text = f"↳ {sub.name}"
    if sub.label:
        text += f" ({sub.label})"
    return text


def rows_units(summary: list[StepRow]) -> Units:
    """Every row's units — the subject's contribution to both sides of the quota
    ratio, so the rendered cells sum to the quest total by construction."""
    total = NO_UNITS
    for row in summary:
        total = total + row.subtree_units
    return total


def units_by_row(summary: list[StepRow]) -> dict[str, Units]:
    """Each row's units by row identity, for a build of the same log that a filter
    narrowed."""
    out: dict[str, Units] = {}
    for row in summary:
        out[row.key] = row.units
        for sub in row.subagents:
            out[sub.key] = sub.units
    return out


def overlay_units(summary: list[StepRow], by_row: dict[str, Units]) -> None:
    """Give each row the units the unfiltered log credited it."""
    for row in summary:
        row.units = by_row.get(row.key, NO_UNITS)
        for sub in row.subagents:
            sub.units = by_row.get(sub.key, NO_UNITS)


def summary_totals(
    summary: list[StepRow], total_seconds: float | None,
    exclusions: Exclusions = NO_EXCLUSIONS,
) -> tuple[float | None, RowUsage]:
    """The totals row: work time (raw wall minus the run's exclusion union,
    floored at 0; None when the run has no total yet) and the accounting sum
    over every step row's whole subtree."""
    work: float | None = (None if total_seconds is None
                          else max(0.0, total_seconds - exclusions.total))
    total = RowUsage()
    for r in summary:
        total = total + r.totals
    return work, total


@dataclass
class SummaryHeader:
    workflow: str
    quest_id: str
    status: str
    total_seconds: float | None
    exclusions: Exclusions = NO_EXCLUSIONS


_HUE_ROTATION: tuple[str, ...] = ("step", "magenta", "cyan", "green")


def summary_total(events: list[dict[str, Any]]) -> float | None:
    if not events:
        return None
    return elapsed_seconds(events[0].get("timestamp"), events[-1].get("timestamp"))


def _header_line(h: SummaryHeader, *, color: bool) -> str:
    workflow = pal.ansi(h.workflow, *pal.NODE_COLORS["workflow"], enabled=color)
    status = pal.ansi(display_word(h.status), *STATUS_COLORS.get(h.status, ()), enabled=color)
    return f"{workflow}  ·  {h.quest_id}  ·  {status}"


_TITLES = ("time", "in", "out", "cost")
_TOKENS_TITLE = "tokens"
_QUOTA_TITLE = "quota"
_WEEKLY_TITLE = "weekly"
_TOTAL_LABEL = "total"


def _last_cell(usage: RowUsage, units: Units,
               column: QuotaColumn | None) -> str:
    """The right-most column: dollars, or the row's share of the weekly window
    once a gauge resolves — on a subscription no money was spent."""
    return cost_cell(usage) if column is None else column.cell(units)


def _column_widths(summary: list[StepRow], open_label: str,
                   total_work: float | None, total_usage: RowUsage,
                   total_last: str,
                   column: QuotaColumn | None) -> tuple[int, int, int, int, int]:
    """Widths measured across step rows, subagent rows, the totals row, and title labels."""
    names = [len(r.step) for r in summary]
    names += [len(_sub_body(s)) + 2 for r in summary for s in r.subagents]
    names.append(len(_TOTAL_LABEL))
    durs = [len(_dur_text(total_work)), len(_TITLES[0])]
    ins = [len(_fmt_tokens(total_usage.input_total)), len(_TITLES[1])]
    outs = [len(_fmt_tokens(total_usage.output)), len(_TITLES[2])]
    lasts = [len(total_last),
             len(_TITLES[3] if column is None else _WEEKLY_TITLE)]
    for r in summary:
        durs.append(len(r.dur_cell(open_label)))
        ins.append(len(_fmt_tokens(r.totals.input_total)))
        outs.append(len(_fmt_tokens(r.totals.output)))
        lasts.append(len(_last_cell(r.totals, r.subtree_units, column)))
        for s in r.subagents:
            durs.append(len(s.dur_cell(open_label)))
            ins.append(len(_fmt_tokens(s.usage.input_total)))
            outs.append(len(_fmt_tokens(s.usage.output)))
            lasts.append(len(_last_cell(s.usage, s.units, column)))
    return (max(names), max(durs), max(ins), max(outs), max(lasts))


def _cells(u: RowUsage, last: str, *, in_w: int, out_w: int,
           last_w: int) -> str:
    """The three numeric cells, right-justified and never coloured."""
    return (f"{_fmt_tokens(u.input_total):>{in_w}}  "
            f"{_fmt_tokens(u.output):>{out_w}}  {last:>{last_w}}")


def _step_line(row: StepRow, *, last: str, hue: str, name_w: int, dur_w: int,
               in_w: int, out_w: int, last_w: int, color: bool,
               open_label: str) -> str:
    codes = pal.NODE_COLORS["error"] if row.errored else (hue,)
    name = pal.ansi(f"{row.step:<{name_w}}", *codes, enabled=color)
    dur = pal.ansi(f"{row.dur_cell(open_label):>{dur_w}}", *codes, enabled=color)
    cells = _cells(row.totals, last, in_w=in_w, out_w=out_w, last_w=last_w)
    return f"  {name}  {dur}  {cells}"


def _sub_line(sub: SubagentRow, *, last: str, name_w: int, dur_w: int, in_w: int,
              out_w: int, last_w: int, color: bool, open_label: str) -> str:
    codes = pal.NODE_COLORS["error"] if sub.errored else pal.NODE_COLORS["subagent"]
    # The extra two-space indent is absorbed by narrowing the label field, so the
    # numeric columns stay aligned with the step rows (see _column_widths).
    body = pal.ansi(f"{_sub_body(sub):<{name_w - 2}}", *codes, enabled=color)
    dur = pal.ansi(f"{sub.dur_cell(open_label):>{dur_w}}", *codes, enabled=color)
    cells = _cells(sub.usage, last, in_w=in_w, out_w=out_w, last_w=last_w)
    return f"    {body}  {dur}  {cells}"


def _title_lines(*, name_w: int, dur_w: int, in_w: int, out_w: int,
                 last_w: int, quota: bool, color: bool) -> list[str]:
    """One title row, or two once the quota column is there to label."""
    time_t, in_t, out_t, cost_t = _TITLES
    lines: list[str] = []
    if quota:
        lines.append(f"  {'':<{name_w}}  {'':>{dur_w}}  "
                     f"{_TOKENS_TITLE:^{in_w + 2 + out_w}}  "
                     f"{_QUOTA_TITLE:>{last_w}}")
        # The footnote marker hangs one column past the numeric edge.
        lines.append(f"  {'':<{name_w}}  {time_t:>{dur_w}}  {in_t:>{in_w}}  "
                     f"{out_t:>{out_w}}  {_WEEKLY_TITLE:>{last_w}}*")
    else:
        lines.append(f"  {'':<{name_w}}  {time_t:>{dur_w}}  {in_t:>{in_w}}  "
                     f"{out_t:>{out_w}}  {cost_t:>{last_w}}")
    return [pal.ansi(ln, *pal.NODE_COLORS["metadata"], enabled=color)
            for ln in lines]


def _total_line(work: float | None, total: RowUsage, last: str, *, name_w: int,
                dur_w: int, in_w: int, out_w: int, last_w: int) -> str:
    # Work time, not a column sum: it includes the gaps between steps.
    cells = _cells(total, last, in_w=in_w, out_w=out_w, last_w=last_w)
    return f"  {_TOTAL_LABEL:<{name_w}}  {_dur_text(work):>{dur_w}}  {cells}"


def render_summary(summary: list[StepRow], *, header: SummaryHeader,
                   color: bool, width: int,
                   quota: QuotaColumn | QuotaFallback | None = None) -> list[str]:
    open_label = open_span_label(header.status)
    work, total = summary_totals(summary, header.total_seconds,
                                 header.exclusions)
    column = quota if isinstance(quota, QuotaColumn) else None
    # The total's share is taken over the rows rendered, so it agrees with the
    # column above it under a windowing flag; unfiltered they are the same sum.
    total_last = (cost_cell(total) if column is None
                  else column.cell(rows_units(summary)))
    name_w, dur_w, in_w, out_w, last_w = _column_widths(
        summary, open_label, work, total, total_last, column)
    lines = [_header_line(header, color=color)]
    if summary:
        lines += _title_lines(name_w=name_w, dur_w=dur_w, in_w=in_w,
                              out_w=out_w, last_w=last_w,
                              quota=column is not None, color=color)
    cells = [total_last]
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
        last = _last_cell(row.totals, row.subtree_units, column)
        cells.append(last)
        lines.append(_step_line(row, last=last, hue=hues[row.step],
                                name_w=name_w, dur_w=dur_w, in_w=in_w,
                                out_w=out_w, last_w=last_w, color=color,
                                open_label=open_label))
        for sub in row.subagents:
            sub_last = _last_cell(sub.usage, sub.units, column)
            cells.append(sub_last)
            lines.append(_sub_line(sub, last=sub_last, name_w=name_w,
                                   dur_w=dur_w, in_w=in_w, out_w=out_w,
                                   last_w=last_w, color=color,
                                   open_label=open_label))
    lines.append("")
    lines.append(_total_line(work, total, total_last, name_w=name_w,
                             dur_w=dur_w, in_w=in_w, out_w=out_w,
                             last_w=last_w))
    notes = legend_lines(cells) if column is None else [FOOTNOTE]
    if isinstance(quota, QuotaFallback):
        notes += list(quota.legends)
    lines += [pal.ansi(f"  {t}", *pal.NODE_COLORS["metadata"], enabled=color)
              for t in notes]
    return lines
