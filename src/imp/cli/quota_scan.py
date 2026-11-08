# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Cross-project discovery of every quest whose quota facts can fall in a run's
weeks.

The weekly gauge is billed per account, so a quest in any project draws on the
same week. Every project state directory under `imp_home()/projects` is
addressed as a path already found: no `project_state_dir()` claim, no
`ensure_current()` rebuild, no `index.connect()` — a renderer may not write
another project's archive. A directory that cannot be read contributes what it
can and is skipped silently.

Only the week figures cross the seam, never a log."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from imp.cli.quota_share import extract, gauge_weeks, parse_ts
from imp.cli.store import (
    GaugeWeek,
    _iso_from_line,
    _last_event_iso,
    _parse_jsonl,
)
from imp.engine.archive import archive_is_current, index, record
from imp.engine.archive.blobstore import LocalBlobStore
from imp.paths import claimed_state_dir, imp_home

# A week holding a subject instant lies within seven days of it, and so do the
# readings that anchor it; twice that leaves room for a clipped overlapping week.
_REACH = timedelta(days=14)


def scan_weeks(control_root: Path, quest_id: str,
               subject_events: list[dict[str, Any]]) -> list[GaugeWeek]:
    """Every gauge-week within reach of the subject's own facts, folding in every
    other quest's readings and subscription units."""
    subject = extract(subject_events)
    stamps = [*(inc.end for inc in subject.increments),
              *(inc.start for inc in subject.increments if inc.start is not None),
              *(r.when for r in subject.readings)]
    if not stamps:
        return gauge_weeks(subject, [])
    others = [extract(events) for _project, _cid, events in other_logs(
        control_root, quest_id, min(stamps) - _REACH, max(stamps) + _REACH)]
    return gauge_weeks(subject, others)


def other_logs(
    control_root: Path, quest_id: str, start: datetime, end: datetime,
) -> Iterator[tuple[str, str, list[dict[str, Any]]]]:
    """`(project, quest id, events)` for every quest whose activity may touch
    `[start, end]`, each once, except the subject in its own project."""
    own = claimed_state_dir(control_root)
    own_resolved = own.resolve() if own is not None else None
    try:
        pdirs = sorted(p for p in (imp_home() / "projects").iterdir() if p.is_dir())
    except OSError:
        return
    for pdir in pdirs:
        subject = quest_id if _same_dir(pdir, own_resolved) else None
        for cid, events in _project_logs(pdir, start, end):
            if cid != subject:
                yield pdir.name, cid, events


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
    never excludes."""
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
