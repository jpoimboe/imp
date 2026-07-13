"""Read-time analysis of a quest's events.jsonl for `imp events`: 1-based
`#N` ordinals (physical line position), suppression of non-rendering rows,
range coalescing/formatting/parsing, step windows, subagent handles, index
and roster building, and the compact digest line. Pure functions over
already-parsed event dicts — no CLI, no filesystem."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
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
