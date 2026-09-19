# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Cross-project discovery of the quests that shared a run's quota window.

The weekly gauge is billed per account, so a concurrent quest in any project
consumes the same window. Every project state directory under
`imp_home()/projects` is addressed as a path already found: no
`project_state_dir()` claim, no `ensure_current()` rebuild, no
`index.connect()` — a renderer may not write another project's archive. A
directory that cannot be read contributes what it can and is skipped silently.

Only the span slice crosses the seam, never a whole log."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from imp.cli.quota_share import parse_ts
from imp.cli.store import (
    NeighbourQuota,
    _iso_from_line,
    _last_event_iso,
    _parse_jsonl,
)
from imp.engine.archive import archive_is_current, index, record
from imp.engine.archive.blobstore import LocalBlobStore
from imp.paths import claimed_state_dir, imp_home

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
    start, end = parse_ts(start_iso), parse_ts(end_iso)
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
        when = parse_ts(e.get("timestamp"))
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
                row = _projected(e)
                # receiver cannot re-derive a patch's instant from the slice alone
                if etype == "agent_message_usage_patch":
                    row["timestamp"] = when.isoformat()
                usage.append(row)
    if not readings and not usage:
        return None
    anchors = [anchor[p] for p in sorted(anchored) if p in anchor]
    needed = {_agent(e) for e in (*usage, *anchors)}
    return NeighbourQuota(
        project=project, quest_id=quest_id,
        readings=tuple(readings), usage=tuple(usage), anchors=tuple(anchors),
        agents={a: triples[a] for a in sorted(needed) if a in triples})


def scan_neighbours(
    control_root: Path, quest_id: str, start_iso: str, end_iso: str,
) -> list[NeighbourQuota]:
    """The in-span slice of every quest in every discovered project except the
    subject quest, whose own units are counted from its own log."""
    start, end = parse_ts(start_iso), parse_ts(end_iso)
    if start is None or end is None:
        return []
    own = claimed_state_dir(control_root)
    own_resolved = own.resolve() if own is not None else None
    try:
        pdirs = sorted(p for p in (imp_home() / "projects").iterdir() if p.is_dir())
    except OSError:
        return []
    out: list[NeighbourQuota] = []
    for pdir in pdirs:
        subject = quest_id if _same_dir(pdir, own_resolved) else None
        for cid, events in _project_logs(pdir, start, end):
            if cid == subject:
                continue
            found = neighbour_slice(pdir.name, cid, events, start_iso, end_iso)
            if found is not None:
                out.append(found)
    return out


def _same_dir(pdir: Path, own_resolved: Path | None) -> bool:
    if own_resolved is None:
        return False
    try:
        return pdir.resolve() == own_resolved
    except OSError:
        return False


def _project_logs(
    pdir: Path, start: datetime, end: datetime,
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Each quest once, live home first — the same precedence `_load_events_for_id`
    keeps. A home already durable in the archive stays on disk until a daemon for
    that project reaps it, so both copies are readable at once and counting them
    twice would deflate the subject's share."""
    seen: set[str] = set()
    for cid, events in _live_logs(pdir / "quests", start, end):
        seen.add(cid)
        yield cid, events
    for cid, events in _archived_logs(pdir / "archive", start, end):
        if cid not in seen:
            yield cid, events


def _window_intersects(
    first: str | None, last: str | None, start: datetime, end: datetime,
) -> bool:
    """Cheap prefilter over a quest's known activity window: an unknown endpoint
    never excludes, so the slice stays the authoritative test."""
    lo, hi = parse_ts(first), parse_ts(last)
    if lo is not None and lo > end:
        return False
    return not (hi is not None and hi < start)


def _live_logs(
    quests: Path, start: datetime, end: datetime,
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    try:
        homes = sorted(p for p in quests.iterdir() if (p / "quest.json").is_file())
    except OSError:
        return
    for home in homes:
        events_path = home / "events.jsonl"
        try:
            window = (_first_event_iso(events_path), _last_event_iso(events_path))
        except OSError:
            continue
        if not _window_intersects(*window, start, end):
            continue
        try:
            yield home.name, _parse_jsonl(events_path.read_bytes())
        except OSError:
            continue


def _first_event_iso(events_path: Path) -> str | None:
    """Timestamp of the log's first timestamped line, read without parsing the
    rest — the head of the window `_last_event_iso` closes."""
    try:
        with events_path.open("rb") as fh:
            for raw in fh:
                ts = _iso_from_line(raw.decode("utf-8", errors="replace"))
                if ts is not None:
                    return ts
    except OSError:
        return None
    return None


def _archived_logs(
    adir: Path, start: datetime, end: datetime,
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    if not archive_is_current(adir):
        return
    conn = index.open_readonly(adir / "index.db")
    if conn is None:
        return
    try:
        spans = index.quest_spans(conn)
    except sqlite3.DatabaseError:
        return
    finally:
        conn.close()
    blobs = LocalBlobStore(adir / "blobs")
    for cid, created, last in spans:
        if not _window_intersects(created, last, start, end):
            continue
        try:
            rec = record.read_record(adir / "records", cid)
            raw = blobs.get(str((rec or {}).get("events_hash") or ""))
        except (OSError, ValueError):
            continue
        if raw is not None:
            yield cid, _parse_jsonl(raw)
