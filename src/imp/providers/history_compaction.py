# src/imp/providers/history_compaction.py
"""Cold-cache tool-result eviction (spec: evict-tool-results-when-cache-cold).

A provider-neutral core. Providers decode their own conversation history into an
ordered sequence of HistoryRecord, ask results_to_evict which tool-result ids to
clear, and re-encode by swapping each matched body for EVICTION_PLACEHOLDER while
preserving every structural field. The core returns ids, never transformed
records, so each provider keeps full control of its own wire shape.

Imports only stdlib plus the two tunables in limits.py (stdlib + anyio, never a
provider module), so no provider import cycle can form.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from imp.agents.tools.limits import CACHE_COLD_THRESHOLD_S, KEEP_RECENT_TURNS

COMPACTABLE_TOOLS: frozenset[str] = frozenset(
    {"read_file", "grep", "glob", "bash", "bash_output"}
)

EVICTION_PLACEHOLDER = "[old tool result cleared]"


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    """One neutral conversation record. A record may be a tool call (call_id +
    call_name set), a tool result (result_id + result_body set), both, or
    neither (plain text). result_id is the call/use id the result correlates to."""

    turn: int
    call_id: str | None = None
    call_name: str | None = None
    result_id: str | None = None
    result_body: str | None = None


def results_to_evict(
    records: Sequence[HistoryRecord],
    *,
    cold: bool,
    keep_recent_turns: int = KEEP_RECENT_TURNS,
) -> frozenset[str]:
    if not cold or not records:
        return frozenset()
    call_names = {r.call_id: r.call_name for r in records if r.call_id is not None}
    cutoff = max(r.turn for r in records) - keep_recent_turns
    evict: set[str] = set()
    for r in records:
        if r.result_id is None or r.turn >= cutoff:
            continue
        if call_names.get(r.result_id) in COMPACTABLE_TOOLS:
            evict.add(r.result_id)
    return frozenset(evict)


def is_cold(
    events_path: Path,
    *,
    now: datetime | None = None,
    threshold_s: float = CACHE_COLD_THRESHOLD_S,
) -> bool:
    last = _last_llm_response_timestamp(events_path)
    if last is None:
        return False
    current = now if now is not None else datetime.now(UTC)
    return (current - last).total_seconds() > threshold_s


def _last_llm_response_timestamp(events_path: Path) -> datetime | None:
    for line in reversed(events_path.read_text().splitlines()):
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("type") == "llm_response":
            ts = rec.get("timestamp")
            return datetime.fromisoformat(ts) if ts else None
    return None
