# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Read-time analysis of a quest's events.jsonl for `imp events`: 1-based
`#N` ordinals (physical line position), suppression of non-rendering rows,
range coalescing/formatting/parsing, step windows, subagent handles, index
and roster building, and the compact digest line. Pure functions over
already-parsed event dicts — no CLI, no filesystem."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from imp.engine.events_digest import summarize

_SUPPRESSED_TYPES = {"agent_message_usage_patch"}


class RangeSpecError(ValueError):
    """A malformed <range> selector token."""


def is_suppressed(event: dict[str, Any]) -> bool:
    """True for rows that consume an ordinal but never render and are excluded
    from counts/histograms: `_malformed` lines and envelope-less
    `agent_message_usage_patch` rows."""
    return bool(event.get("_malformed")) or event.get("type") in _SUPPRESSED_TYPES


def coalesce_ranges(ordinals: Iterable[int]) -> list[tuple[int, int]]:
    """Sorted, de-duplicated ordinals collapsed into contiguous inclusive
    (lo, hi) spans."""
    ranges: list[tuple[int, int]] = []
    for n in sorted(set(ordinals)):
        if ranges and n == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], n)
        else:
            ranges.append((n, n))
    return ranges


def format_ranges(ranges: Sequence[tuple[int, int]]) -> str:
    """Render spans as the paste-back selector syntax: `#59-120, #210-255`."""
    parts = [f"#{lo}" if lo == hi else f"#{lo}-{hi}" for lo, hi in ranges]
    return ", ".join(parts)


def parse_range_spec(spec: str, max_ordinal: int) -> list[tuple[int, int]]:
    """Parse a comma-separated selector of ordinals and ranges with open ends
    (`42`, `42,55,60`, `59-120`, `59-120,210-255`, `59-`, `-80`) into inclusive
    (lo, hi) intervals. An open low end resolves to 1; an open high end to
    `max_ordinal`. A leading `#` and surrounding whitespace are tolerated so a
    printed range pastes back verbatim."""
    intervals: list[tuple[int, int]] = []
    for token in spec.split(","):
        token = token.strip().lstrip("#")
        if not token:
            continue
        try:
            if "-" in token:
                lo_s, hi_s = token.split("-", 1)
                lo = int(lo_s) if lo_s.strip() else 1
                hi = int(hi_s) if hi_s.strip() else max_ordinal
            else:
                lo = hi = int(token)
        except ValueError as exc:
            raise RangeSpecError(f"invalid range {spec!r}") from exc
        intervals.append((lo, hi))
    return intervals


def in_intervals(n: int, intervals: Sequence[tuple[int, int]]) -> bool:
    return any(lo <= n <= hi for lo, hi in intervals)


@dataclass
class StepInfo:
    name: str
    windows: list[tuple[int, int]] = field(default_factory=list)

    def occurrences(self) -> int:
        return len(self.windows)


def _step_name(event: dict[str, Any]) -> str | None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    name = payload.get("step")
    return name if isinstance(name, str) else None


def _in_windows(ordinal: int, windows: Sequence[tuple[int, int]]) -> bool:
    return in_intervals(ordinal, windows)


def step_windows(indexed: Sequence[tuple[int, dict[str, Any]]]) -> list[StepInfo]:
    """One StepInfo per distinct step name (in first-appearance order), each
    carrying the (start, end) ordinal span of every occurrence. Windows are
    built from step_started/step_completed lifecycle events, so script steps
    (which have no agent) are included. A step still open at the end of the log
    closes at the last ordinal. A re-entry into an already-open step (a
    reincarnation, a transient-error retry, or a restart from an empty
    transcript) emits a second step_started and shares the open window rather
    than resetting its start; once a step has completed, a later start opens a
    separate window."""
    order: list[str] = []
    by_name: dict[str, StepInfo] = {}
    open_start: dict[str, int] = {}

    def _ensure(name: str) -> StepInfo:
        if name not in by_name:
            by_name[name] = StepInfo(name=name)
            order.append(name)
        return by_name[name]

    for ordinal, event in indexed:
        etype = event.get("type")
        if etype not in ("step_started", "step_completed"):
            continue
        name = _step_name(event)
        if name is None:
            continue
        if etype == "step_started":
            _ensure(name)
            open_start.setdefault(name, ordinal)
        else:
            start = open_start.pop(name, ordinal)
            _ensure(name).windows.append((start, ordinal))
    if open_start and indexed:
        last_ord = indexed[-1][0]
        for name, start in open_start.items():
            _ensure(name).windows.append((start, last_ord))
    return [by_name[n] for n in order]


def subagent_handle_map(
    indexed: Sequence[tuple[int, dict[str, Any]]],
) -> dict[str, str]:
    """Map each `sub:<id>` agent_id to its display handle `<name>-<n>`. `<n>` is
    the global dispatch counter within the step: the step's dispatch tool_calls
    (whose event id equals the referenced `<id>`) are ordered by ordinal and
    numbered sequentially across subagent names."""
    by_id: dict[str, dict[str, Any]] = {}
    ord_by_id: dict[str, int] = {}
    for ordinal, event in indexed:
        eid = event.get("id")
        if isinstance(eid, str):
            by_id[eid] = event
            ord_by_id[eid] = ordinal
    handles: dict[str, str] = {}
    for step in step_windows(indexed):
        sub_ids: set[str] = set()
        for ordinal, event in indexed:
            aid = event.get("agent_id") or ""
            if aid.startswith("sub:") and _in_windows(ordinal, step.windows):
                sub_ids.add(aid[len("sub:"):])
        dispatch_ids = sorted(
            (sid for sid in sub_ids if sid in by_id),
            key=lambda sid: ord_by_id[sid],
        )
        for n, sid in enumerate(dispatch_ids, start=1):
            payload = by_id[sid].get("payload")
            src = payload.get("input") if isinstance(payload, dict) else None
            name = (src or {}).get("subagent_name") if isinstance(src, dict) else None
            handles[f"sub:{sid}"] = f"{name or 'sub'}-{n}"
    return handles


@dataclass
class StepRow:
    name: str
    ranges: list[tuple[int, int]]
    count: int
    occurrences: int
    subagents: int


@dataclass
class Index:
    quest_id: str
    total_events: int
    step_count: int
    steps: list[StepRow]
    types: list[tuple[str, int]]


def _type_histogram(
    indexed: Sequence[tuple[int, dict[str, Any]]],
    windows: Sequence[tuple[int, int]] | None = None,
) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for ordinal, event in indexed:
        if is_suppressed(event):
            continue
        if windows is not None and not _in_windows(ordinal, windows):
            continue
        etype = event.get("type") or "?"
        counts[etype] = counts.get(etype, 0) + 1
    # Count-descending; ties keep first-appearance order. `counts` is
    # insertion-ordered and sorted() is stable, so a plain -count key preserves
    # the order types were first seen (spec mandates count-descending only).
    return sorted(counts.items(), key=lambda kv: -kv[1])


def _displayable_count(
    indexed: Sequence[tuple[int, dict[str, Any]]],
    windows: Sequence[tuple[int, int]],
) -> int:
    return sum(
        1 for ordinal, event in indexed
        if not is_suppressed(event) and _in_windows(ordinal, windows)
    )


def _subagent_count(
    indexed: Sequence[tuple[int, dict[str, Any]]],
    windows: Sequence[tuple[int, int]],
) -> int:
    seen: set[str] = set()
    for ordinal, event in indexed:
        aid = event.get("agent_id") or ""
        if aid.startswith("sub:") and _in_windows(ordinal, windows):
            seen.add(aid)
    return len(seen)


def build_index(
    indexed: Sequence[tuple[int, dict[str, Any]]], quest_id: str,
) -> Index:
    steps = step_windows(indexed)
    rows = [
        StepRow(
            name=s.name,
            ranges=sorted(s.windows),
            count=_displayable_count(indexed, s.windows),
            occurrences=s.occurrences(),
            subagents=_subagent_count(indexed, s.windows),
        )
        for s in steps
    ]
    total = sum(1 for _, event in indexed if not is_suppressed(event))
    return Index(
        quest_id=quest_id,
        total_events=total,
        step_count=len(rows),
        steps=rows,
        types=_type_histogram(indexed),
    )


def render_index(index: Index) -> list[str]:
    lines = [
        f"quest {index.quest_id} · {index.total_events} events "
        f"· {index.step_count} steps",
        "",
        "steps:",
    ]
    name_w = max((len(r.name) for r in index.steps), default=0)
    range_w = max((len(format_ranges(r.ranges)) for r in index.steps), default=0)
    for r in index.steps:
        loop = f"  (×{r.occurrences})" if r.occurrences > 1 else ""
        lines.append(
            f"  {r.name:<{name_w}}  {format_ranges(r.ranges):<{range_w}}  "
            f"{r.count}{loop}".rstrip()
        )
        if r.subagents:
            noun = "subagent" if r.subagents == 1 else "subagents"
            lines.append(f"    ↳ {r.subagents} {noun}")
    lines.extend(["", "types:"])
    count_w = max((len(str(c)) for _, c in index.types), default=0)
    for etype, count in index.types:
        lines.append(f"  {count:>{count_w}}  {etype}")
    lines.extend(
        ["", "→ --step <name> · --type <type> · <range> · --all · --json"]
    )
    return lines


@dataclass
class AgentRow:
    handle: str
    ranges: list[tuple[int, int]]
    count: int


@dataclass
class ScopedIndex:
    step_name: str
    windows: list[tuple[int, int]]
    total_events: int
    agents: list[AgentRow]
    types: list[tuple[str, int]]


def build_step_index(
    indexed: Sequence[tuple[int, dict[str, Any]]],
    step_name: str,
    handle_map: dict[str, str],
) -> ScopedIndex | None:
    step = next(
        (s for s in step_windows(indexed) if s.name == step_name), None)
    if step is None:
        return None
    windows = sorted(step.windows)
    own_key = f"__own__:{step_name}"
    buckets: dict[str, list[int]] = {}
    for ordinal, event in indexed:
        if is_suppressed(event) or not _in_windows(ordinal, windows):
            continue
        aid = event.get("agent_id") or ""
        key = aid if aid.startswith("sub:") else own_key
        buckets.setdefault(key, []).append(ordinal)
    agents: list[AgentRow] = []
    if own_key in buckets:
        agents.append(AgentRow(
            handle=step_name,
            ranges=coalesce_ranges(buckets[own_key]),
            count=len(buckets[own_key]),
        ))
    sub_keys = sorted(
        (k for k in buckets if k.startswith("sub:")),
        key=lambda k: min(buckets[k]),
    )
    for key in sub_keys:
        agents.append(AgentRow(
            handle=handle_map.get(key, key),
            ranges=coalesce_ranges(buckets[key]),
            count=len(buckets[key]),
        ))
    return ScopedIndex(
        step_name=step_name,
        windows=windows,
        total_events=_displayable_count(indexed, windows),
        agents=agents,
        types=_type_histogram(indexed, windows),
    )


def render_step_index(index: ScopedIndex) -> list[str]:
    lines = [
        f"step {index.step_name} · {format_ranges(index.windows)} "
        f"· {index.total_events} events",
        "",
        "agents:",
    ]
    handle_w = max((len(a.handle) for a in index.agents), default=0)
    range_w = max(
        (len(format_ranges(a.ranges)) for a in index.agents), default=0)
    for a in index.agents:
        lines.append(
            f"  {a.handle:<{handle_w}}  "
            f"{format_ranges(a.ranges):<{range_w}}  {a.count}".rstrip()
        )
    lines.extend(["", "types:"])
    count_w = max((len(str(c)) for _, c in index.types), default=0)
    for etype, count in index.types:
        lines.append(f"  {count:>{count_w}}  {etype}")
    return lines


_TYPE_WIDTH = 16


def agent_display(agent_id: str | None, handle_map: dict[str, str]) -> str:
    """Column form of an author: `step:<phase>/<name>` → `<name>`; `sub:<id>` →
    its `<name>-<n>` handle (raw `sub:<id>` when unmapped); `None` → blank."""
    if not agent_id:
        return ""
    if agent_id.startswith("sub:"):
        return handle_map.get(agent_id, agent_id)
    if agent_id.startswith("step:"):
        return agent_id.split("/", 1)[-1]
    return agent_id


def usage_patch_overlays(
    indexed: Sequence[tuple[int, dict[str, Any]]],
) -> dict[int, dict[str, Any]]:
    """Ordinal -> codex usage-patch occupancy for that `agent_message` — display only."""
    last: dict[str | None, int] = {}
    overlays: dict[int, dict[str, Any]] = {}
    for ordinal, event in indexed:
        etype = event.get("type")
        if etype == "agent_message":
            last[event.get("agent_id")] = ordinal
        elif etype == "agent_message_usage_patch":
            target = last.get(event.get("agent_id"))
            if target is None:
                continue
            payload = event.get("payload") or {}
            fields = {key: payload[key]
                      for key in ("context_tokens", "context_window")
                      if key in payload}
            if fields:
                overlays.setdefault(target, {}).update(fields)
    return overlays


def digest_line(
    ordinal: int, event: dict[str, Any], handle_map: dict[str, str],
    *, width: int | None = None, overlay: dict[str, Any] | None = None,
) -> str:
    """Compact one-line rendering: `#N  <type>  [agent]  <summary>` — no
    wall-clock timestamp (the ordinal conveys order). When `width` is an int the
    assembled line is clipped to that many columns (one reserved for a trailing
    `…`); `width=None` renders the full untruncated line (the `--full` path).
    `overlay` adds payload fields for this rendering only — a copy is summarized,
    so the event dict stays exactly as it was read."""
    etype = event.get("type") or "?"
    agent = agent_display(event.get("agent_id"), handle_map)
    if overlay:
        payload = event.get("payload")
        base = payload if isinstance(payload, dict) else {}
        event = {**event, "payload": {**base, **overlay}}
    line = (
        f"#{ordinal}  {etype:<{_TYPE_WIDTH}} [{agent}]  {summarize(event)}"
    ).rstrip()
    if width is not None and len(line) > width:
        line = line[: width - 1] + "…"
    return line


def _agent_matches(
    event: dict[str, Any],
    selectors: set[str],
    handle_map: dict[str, str],
) -> bool:
    aid = event.get("agent_id") or ""
    display = agent_display(aid, handle_map)
    for sel in selectors:
        # Exact match on the display handle (step name / <name>-<n>) or the raw
        # agent_id; substring only for an explicit sub:<id> selector (partial
        # uuid), so a step-name/handle selector never over-matches.
        if sel in (display, aid):
            return True
        if sel.startswith("sub:") and sel in aid:
            return True
    return False


def select_event_lines(
    indexed: Sequence[tuple[int, dict[str, Any]]],
    *,
    types: set[str] | None = None,
    subtypes: set[str] | None = None,
    agents: set[str] | None = None,
    ranges: Sequence[tuple[int, int]] | None = None,
    step_names: set[str] | None = None,
    all_: bool = False,
    limit: int | None = None,
    handle_map: dict[str, str],
) -> list[tuple[int, dict[str, Any]]]:
    """Displayable events matching every provided filter (AND across filter
    kinds; OR within a multi-valued one). `step_names` scopes to the union of
    those steps' windows. `all_` adds no constraint. `limit` keeps the last N
    of the result. Suppressed rows never appear but keep their ordinal."""
    windows: list[tuple[int, int]] | None = None
    if step_names:
        windows = []
        for step in step_windows(indexed):
            if step.name in step_names:
                windows.extend(step.windows)
    out: list[tuple[int, dict[str, Any]]] = []
    for ordinal, event in indexed:
        if is_suppressed(event):
            continue
        if windows is not None and not _in_windows(ordinal, windows):
            continue
        if types is not None and (event.get("type") or "?") not in types:
            continue
        if subtypes is not None and \
                (event.get("payload") or {}).get("subtype") not in subtypes:
            continue
        if agents is not None and not _agent_matches(event, agents, handle_map):
            continue
        if ranges is not None and not in_intervals(ordinal, ranges):
            continue
        out.append((ordinal, event))
    if limit is not None and limit > 0:
        out = out[-limit:]
    return out


def index_json(index: Index) -> dict[str, Any]:
    return {
        "quest_id": index.quest_id,
        "total_events": index.total_events,
        "step_count": index.step_count,
        "steps": [
            {
                "name": r.name,
                "ranges": [[lo, hi] for lo, hi in r.ranges],
                "count": r.count,
                "occurrences": r.occurrences,
                "subagents": r.subagents,
            }
            for r in index.steps
        ],
        "types": [{"type": t, "count": c} for t, c in index.types],
    }


def step_index_json(index: ScopedIndex) -> dict[str, Any]:
    return {
        "step": index.step_name,
        "windows": [[lo, hi] for lo, hi in index.windows],
        "total_events": index.total_events,
        "agents": [
            {
                "handle": a.handle,
                "ranges": [[lo, hi] for lo, hi in a.ranges],
                "count": a.count,
            }
            for a in index.agents
        ],
        "types": [{"type": t, "count": c} for t, c in index.types],
    }


def events_json(
    selected: Sequence[tuple[int, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Full structured payloads (with the wall-clock timestamp) plus the `#N`
    ordinal, one object per selected event."""
    return [{**event, "ordinal": ordinal} for ordinal, event in selected]
