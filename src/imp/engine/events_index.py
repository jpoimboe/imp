"""Read-time analysis of a quest's events.jsonl for `imp events`: 1-based
`#N` ordinals (physical line position), suppression of non-rendering rows,
range coalescing/formatting/parsing, step windows, subagent handles, index
and roster building, and the compact digest line. Pure functions over
already-parsed event dicts — no CLI, no filesystem."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

_SUPPRESSED_TYPES = {"llm_response_usage_patch"}


def is_suppressed(event: dict[str, Any]) -> bool:
    """True for rows that consume an ordinal but never render and are excluded
    from counts/histograms: `_malformed` lines and envelope-less
    `llm_response_usage_patch` rows."""
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
        if "-" in token:
            lo_s, hi_s = token.split("-", 1)
            lo = int(lo_s) if lo_s.strip() else 1
            hi = int(hi_s) if hi_s.strip() else max_ordinal
        else:
            lo = hi = int(token)
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
    closes at the last ordinal."""
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
            open_start[name] = ordinal
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
