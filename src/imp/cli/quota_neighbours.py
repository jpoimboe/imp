# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""The in-span quota slice of one quest's event log.

Only this slice crosses the `Store` seam, never a whole log: a busy week's
events run to tens of megabytes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from imp.cli.store import NeighbourQuota

_USAGE_TYPES = ("query_result", "agent_message", "agent_message_usage_patch")

_Triple = tuple[str | None, str | None, str | None]

_ENVELOPE = ("id", "type", "timestamp", "agent_id")

# The payload fields the units pipeline reads, and nothing else. `agent_message`
# otherwise carries `raw`, the verbatim provider response whose leading content
# block is the model's thinking text -- two thirds of a real slice's bytes, and
# prose a renderer has no business pulling across the seam.
_PAYLOAD = ("provider", "model", "auth_mode", "usage", "model_usage",
            "total_cost_usd", "duration_ms", "session_id", "context_tokens",
            "windows")


def _projected(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    kept = {k: event[k] for k in _ENVELOPE if k in event}
    kept["payload"] = {k: payload[k] for k in _PAYLOAD if k in payload}
    return kept


def _ts(value: Any) -> datetime | None:
    """A foreign log's timestamp as a comparable instant; a naive one reads UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _agent(event: dict[str, Any]) -> str:
    aid = event.get("agent_id")
    return aid if isinstance(aid, str) else ""


def neighbour_slice(
    project: str, quest_id: str, events: list[dict[str, Any]],
    start_iso: str, end_iso: str,
) -> NeighbourQuota | None:
    """One quest's in-span quota slice, or None when it holds no in-span reading
    or usage row — which is what "activity intersects the span" means here.

    `agent_message_usage_patch` carries no timestamp of its own (state.py writes
    it with three keys), so its log position is its instant: it takes the last
    timestamp seen before it, which is where the quest's own accounting credits
    it too. Its `agent_message` may therefore precede the span while the patch
    falls inside it. A `query_result` after the span end is dropped rather than
    prorated: the span ends at the subject's last event, so only a neighbour's
    in-flight query can straddle it."""
    start, end = _ts(start_iso), _ts(end_iso)
    if start is None or end is None:
        return None
    readings: list[dict[str, Any]] = []
    usage: list[dict[str, Any]] = []
    triples: dict[str, _Triple] = {}
    anchor: dict[tuple[str, str], dict[str, Any]] = {}
    anchored: set[tuple[str, str]] = set()
    latest: datetime | None = None
    for e in events:
        etype = e.get("type")
        aid = _agent(e)
        when = _ts(e.get("timestamp"))
        if etype == "agent_message_usage_patch":
            when = latest
        elif when is not None:
            latest = when
        if etype == "agent_message" and when is not None and when <= end:
            p = e.get("payload") or {}
            triples[aid] = (p.get("provider"), p.get("model"),
                            p.get("auth_mode"))
        if when is None:
            continue
        inside = start <= when <= end
        if etype == "quota_reading":
            if inside:
                readings.append(_projected(e))
        elif etype in _USAGE_TYPES:
            if etype == "query_result":
                pair = (aid, str((e.get("payload") or {}).get("session_id") or ""))
                if inside:
                    anchored.add(pair)
                elif when < start:
                    anchor[pair] = _projected(e)
            if inside:
                usage.append(_projected(e))
    if not readings and not usage:
        return None
    anchors = [anchor[p] for p in sorted(anchored) if p in anchor]
    needed = {_agent(e) for e in (*usage, *anchors)}
    return NeighbourQuota(
        project=project, quest_id=quest_id,
        readings=tuple(readings), usage=tuple(usage), anchors=tuple(anchors),
        agents={a: triples[a] for a in sorted(needed) if a in triples})
