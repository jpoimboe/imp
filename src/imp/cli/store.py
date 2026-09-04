# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Read-only introspection seam the CLI routes its reads through."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from imp.paths import project_state_dir


@dataclass(frozen=True)
class CompletedRow:
    quest_id: str
    status: str
    step: str
    last: str
    title: str
    created: str


@dataclass(frozen=True)
class Readiness:
    ready: bool
    blocked_on: tuple[str, ...]


class HomeDigest(NamedTuple):
    quest_id: str
    quest_json: bytes
    state_json: bytes | None
    meta: dict[str, Any]
    last_iso: str
    has_user_message: bool
    prose: str
    intent_title: str | None
    lock_live: bool


@dataclass(frozen=True)
class QuestCard:
    quest_id: str
    status: str
    step: str
    last: str
    title: str
    created: str
    step_label: str | None
    phase_label: str | None
    base_branch: str | None
    merge_commit: str | None
    parent: dict[str, str] | None
    depends_on: list[dict[str, Any]]
    intent: str | None
    summary: bytes | None
    vars: dict[str, object]


class _BacklogMatch(NamedTuple):
    quest_id: str
    title: str
    seed: str
    last_activity: str


@dataclass(frozen=True)
class SearchHit:
    quest_id: str
    title: str
    zone: str
    content: str
    last_activity: str | None


@dataclass(frozen=True)
class SearchZones:
    active: bool
    backlog: bool
    statuses: frozenset[str]
    merged_after: str | None


class Store(Protocol):
    def get_events(self, quest_id: str) -> list[dict[str, Any]] | None: ...
    def get_artifact(self, quest_id: str, kind: str) -> bytes | None: ...
    def completed_rows(self) -> list[CompletedRow]: ...
    def backlog_readiness(self, depends_on: list[str]) -> Readiness: ...
    def scan_homes(self) -> list[HomeDigest]: ...
    def quest_card(self, quest_id: str) -> QuestCard | None: ...
    def search(self, query: str, zones: SearchZones) -> list[SearchHit]: ...
    def search_event_candidates(self, query: str) -> list[tuple[str, str, str, str | None]]: ...


class LocalStore:
    def __init__(self, control_root: Path) -> None:
        self.control_root = control_root

    def get_events(self, quest_id: str) -> list[dict[str, Any]] | None:
        return _load_events_for_id(self.control_root, quest_id)

    def get_artifact(self, quest_id: str, kind: str) -> bytes | None:
        return _show_artifact(self.control_root, quest_id, f"{kind}/{quest_id}.md")

    def completed_rows(self) -> list[CompletedRow]:
        from imp.engine.archive import read as archive_read
        return [CompletedRow(r[0], r[1], r[2], r[3], r[4], r[5])
                for r in archive_read.completed_rows(self.control_root)]

    def backlog_readiness(self, depends_on: list[str]) -> Readiness:
        from imp.engine import backlog
        ready, unmerged = backlog.readiness(self.control_root, depends_on)
        return Readiness(ready, tuple(unmerged))

    def scan_homes(self) -> list[HomeDigest]:
        from imp.cli.render_helpers import conversation_prose
        from imp.engine.quest import LAYOUT_VERSION, migrate
        from imp.engine.state import ProjectLock, lock_is_live

        out: list[HomeDigest] = []
        for quest_id in _list_quest_homes(self.control_root):
            cdir = _quest_home(self.control_root, quest_id)
            try:
                quest_json = (cdir / "quest.json").read_bytes()
                meta = json.loads(quest_json)
            except (FileNotFoundError, json.JSONDecodeError):
                continue
            if meta.get("layout_version", 0) != LAYOUT_VERSION:
                try:
                    with ProjectLock(cdir / "lock").acquire(timeout=0.0):
                        migrate(meta.get("layout_version", 0), LAYOUT_VERSION, cdir)
                    quest_json = (cdir / "quest.json").read_bytes()
                    meta = json.loads(quest_json)
                except Exception:
                    pass  # LockHeld (live) or migration error: keep raw quest.json bytes
            try:
                state_json: bytes | None = (cdir / "state" / "state.json").read_bytes()
            except FileNotFoundError:
                state_json = None
            events_path = cdir / "events.jsonl"
            last_iso = _last_event_iso(events_path) or ""
            has_um = _events_have_user_message(events_path)
            try:
                parsed = _parse_jsonl(events_path.read_bytes())
            except OSError:
                parsed = []
            out.append(HomeDigest(
                quest_id, quest_json, state_json, meta, last_iso, has_um,
                conversation_prose(parsed), _resolve_intent(parsed),
                lock_is_live(cdir / "lock")))
        return out

    def quest_card(self, quest_id: str) -> QuestCard | None:
        from imp.engine import quest_refs
        row = _row_for_id(self.control_root, quest_id)
        if row is None:
            return None
        _id, status, step, last, title, created, step_label, phase_label = row[:8]
        events = _load_events_for_id(self.control_root, quest_id) or []
        meta = _quest_meta(self.control_root, quest_id) or {}
        state = _quest_state(self.control_root, quest_id) or {}
        return QuestCard(
            quest_id=_id, status=status, step=step, last=last, title=title,
            created=created, step_label=step_label, phase_label=phase_label,
            base_branch=meta.get("base_branch"),
            merge_commit=state.get("merge_commit"),
            parent=quest_refs.resolve_parent(self.control_root, meta.get("parent")),
            depends_on=quest_refs.resolve_dependencies(
                self.control_root, list(meta.get("depends_on", []))),
            intent=_resolve_intent(events),
            summary=_show_artifact(self.control_root, quest_id, f"summary/{quest_id}.md"),
            vars=dict(state.get("vars") or {}),
        )


    def search(self, query: str, zones: SearchZones) -> list[SearchHit]:
        hits: list[SearchHit] = []
        if zones.active:
            hits += _search_active(self.control_root, query)
        if zones.backlog:
            hits += _search_backlog(self.control_root, query)
        if zones.statuses:
            hits += _search_archived(
                self.control_root, query, set(zones.statuses), zones.merged_after)
        return hits

    def search_event_candidates(self, query: str) -> list[tuple[str, str, str, str | None]]:
        from imp.engine.archive import read as archive_read
        return archive_read.search_archive_events_content(self.control_root, query)


# Terminal status -> resumable. Explicit, never the _INFLIGHT complement: archiving a
# shelved home non-resumably would drop its step blobs, so an unknown status is skipped.
_TERMINAL_ARCHIVE_RESUMABLE: dict[str, bool] = {
    "merged": False, "done": False, "abandoned": False, "shelved": True,
}


def _archive_stranded_home(
    control: Path, quest_id: str, home: Path, state_json: bytes
) -> None:
    """Finish archiving a terminal home whose process died before it could."""
    from imp.engine import archive
    from imp.engine.archive import read as archive_read
    resumable = _TERMINAL_ARCHIVE_RESUMABLE.get(json.loads(state_json).get("status"))
    if resumable is None or archive_read.is_archived(control, quest_id):
        return
    archive.archive_quest(
        control, quest_id,
        events_path=home / "events.jsonl",
        quest_json_path=home / "quest.json",
        state_json_path=home / "state" / "state.json",
        steps_dir=(home / "steps") if resumable else None,
        resumable=resumable)


def make_store(control_root: Path) -> Store:
    sock = os.environ.get("IMP_SESSION_SOCKET")
    if sock:
        from imp.cli.store_proxy import ProxyStore
        return ProxyStore(sock)
    return LocalStore(control_root)


def _list_quest_homes(control: Path) -> list[str]:
    """Ids of quests with a permanent home under <state dir>/quests/."""
    from imp.engine.quest import live_quest_ids
    return live_quest_ids(control)


def _quest_home(control: Path, quest_id: str) -> Path:
    return project_state_dir(control) / "quests" / quest_id


def _parse_jsonl(raw: bytes) -> list[dict[str, Any]]:
    """Parse journaled events.jsonl bytes into event dicts, mirroring
    JsonlEventLog.read: malformed lines become {"_malformed": True, ...}."""
    out: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"_malformed": True, "raw": line})
    return out


def _show_artifact(control: Path, quest_id: str, rel: str) -> bytes | None:
    from imp.engine.archive import read as archive_read
    live = _quest_home(control, quest_id) / "artifacts" / rel
    if live.is_file():
        return live.read_bytes()
    return archive_read.read_artifact(control, quest_id, rel)


def _user_message_text(payload: dict[str, Any]) -> str:
    return "".join(
        b.get("text", "") for b in (payload.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "text")


def _first_genuine_human_turn(events: list[dict[str, Any]]) -> str | None:
    """First reply/interject a human actually typed. The opening intent is
    resolved separately (by _resolve_intent), so it is skipped here; every
    non-intent user_message is, by construction, a genuine human turn."""
    for ev in events:
        if ev.get("type") != "user_message":
            continue
        if (ev.get("payload") or {}).get("subtype") == "intent":
            continue
        text = _user_message_text(ev.get("payload") or {}).strip()
        if text:
            return text
    return None


def _resolve_intent(events: list[dict[str, Any]]) -> str | None:
    for ev in events:
        if ev.get("type") == "user_message" and \
                (ev.get("payload") or {}).get("subtype") == "intent":
            text = _user_message_text(ev.get("payload") or {}).strip()
            if text:
                return text
    return _first_genuine_human_turn(events)


def _quest_meta(control: Path, quest_id: str) -> dict[str, Any] | None:
    from imp.engine.archive import read as archive_read
    cdir = _quest_home(control, quest_id)
    raw: bytes | None
    if (cdir / "quest.json").exists():
        raw = (cdir / "quest.json").read_bytes()
    else:
        raw = archive_read.read_quest_json(control, quest_id)
    if raw is None:
        return None
    try:
        result: dict[str, Any] = json.loads(raw)
        return result
    except json.JSONDecodeError:
        return None


def _quest_state(control: Path, quest_id: str) -> dict[str, Any] | None:
    from imp.engine.archive import read as archive_read
    cdir = _quest_home(control, quest_id)
    sj = cdir / "state" / "state.json"
    raw = sj.read_bytes() if sj.exists() else archive_read.read_state_json(control, quest_id)
    if raw is None:
        return None
    try:
        result: dict[str, Any] = json.loads(raw)
        return result
    except json.JSONDecodeError:
        return None


def _is_backlog_home(
    meta: dict[str, Any], *, conversed: bool, lock_live: bool
) -> bool:
    """Shared zone predicate. A live quest home is Backlog iff it is
    un-materialized, has a non-draft origin, has never been conversed (no
    user_message event), and is not lock-live. The detail card
    (`_unstarted_backlog_meta`) and both overview zone builders (`_status_rows`,
    `_backlog_rows`) classify through this one helper, so a resumed/lock-live
    seed leaves Backlog and the three classifiers cannot drift."""
    return (
        not meta.get("materialized", True)
        and meta.get("origin", "draft") != "draft"
        and not conversed
        and not lock_live
    )


def _load_events_for_id(control: Path, quest_id: str) -> list[dict[str, Any]] | None:
    """Worktree first, archive second. Returns None if the id is in neither.

    Worktree-first is necessary, not merely preferred: an id lives in exactly
    one place (the archive snapshot is written once, concurrent with worktree
    removal), so the only overlap is a crash mid-sequence where worktree-first
    harmlessly returns identical content."""
    from imp.engine.archive import read as archive_read
    from imp.engine.errors import LayoutMismatch, LoadError, MigrationLockHeld
    from imp.engine.quest import Quest
    try:
        quest = Quest.load(control, quest_id, migrate_if_lockable=True)
    except (LayoutMismatch, MigrationLockHeld):
        ev_path = _quest_home(control, quest_id) / "events.jsonl"
        if ev_path.exists():
            return _parse_jsonl(ev_path.read_bytes())
        quest = None
    except LoadError:
        quest = None
    if quest is not None:
        return list(quest.event_log().read())
    raw = archive_read.read_events(control, quest_id)
    if raw is None:
        return None
    return _parse_jsonl(raw)


def _last_nonempty_line(data: bytes) -> bytes | None:
    """Last non-empty line of `data` with trailing whitespace removed, or None
    when there is no non-empty line. Avoids splitting the whole buffer: a single
    rstrip + rfind, used by the journal path (full blob already in memory) and
    by the filesystem tail reader."""
    stripped = data.rstrip()
    if not stripped:
        return None
    nl = stripped.rfind(b"\n")
    return stripped[nl + 1:]


_TAIL_CHUNK = 64 * 1024


def _read_last_line(events_path: Path) -> bytes | None:
    """Last non-empty line of `events_path`, read from the end without loading
    the whole file. Returns None when the file has no non-empty line. Defined to
    return exactly `_last_nonempty_line(events_path.read_bytes())` — it reads
    backward in 64 KiB chunks only until the last line is fully in hand. Raises
    FileNotFoundError if the file is absent (the caller maps that to '-')."""
    with events_path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        buf = b""
        while pos > 0:
            step = min(_TAIL_CHUNK, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            stripped = buf.rstrip()
            if not stripped:
                continue  # only trailing whitespace seen so far
            nl = stripped.rfind(b"\n")
            if nl != -1:
                return stripped[nl + 1:]
            # No newline yet: the last line starts at or before `pos`; if pos
            # has reached 0 the loop ends and the final return handles it.
        return _last_nonempty_line(buf)


def _iso_from_line(line: str) -> str | None:
    if not line.strip():
        return None
    try:
        return str(json.loads(line)["timestamp"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _last_event_iso(events_path: Path) -> str | None:
    try:
        line = _read_last_line(events_path)
    except FileNotFoundError:
        return None
    if line is None:
        return None
    return _iso_from_line(line.decode("utf-8", errors="replace"))


def _journal_last_iso(control: Path, quest_id: str) -> str | None:
    for r in make_store(control).completed_rows():
        if r.quest_id == quest_id:
            return r.last or None
    return None


# Spec §10: in-flight statuses; completed quests live in the archive.
_INFLIGHT = ("running", "waiting", "error", "new", "paused", "merge_blocked")


def resolve_display_status(
    status: str, *, lock_live: bool, halt_condition: str | None = None
) -> str:
    """The one total map from persisted state to the resolved display word.

    Liveness is resolved by the caller (daemon-side, where the lock is visible)
    and handed in as `lock_live`: live + a `waiting` halt-condition reads
    `waiting` (parked at a human gate); live otherwise reads `running`. Not live,
    every stored word passes through unchanged (stored == displayed). The resolved
    word is what the membership gates and the STATUS_COLORS key consume, so
    `merge_blocked` stays underscored here."""
    return ("waiting" if halt_condition == "waiting" else "running") if lock_live else status


# Row = (id, status, step, last_activity, title, created_at, step_label,
# phase_label, started_at). created_at is a sort key; started_at (persisted,
# may be None) keys the in-flight ascending sort. step_label + phase_label feed
# the merged STATUS cell's ` (phase · step)` suffix (see quest_row). The step
# slot stays the RAW step name so imp list keeps printing it.
_Row = tuple[str, str, str, str, str, str, str | None, str | None, str | None]


def _events_have_user_message(events_path: Path) -> bool:
    """True iff events.jsonl holds at least one user_message event. Read line by
    line and short-circuit so a long log isn't fully parsed just to classify."""
    try:
        with events_path.open("rb") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "user_message":
                    return True
    except FileNotFoundError:
        return False
    return False


def _status_row(quest_id: str, state_json: bytes, quest_json: bytes,
                last_activity: str) -> _Row | None:
    """Pure (id, status, step, last_activity, title, created_at) extractor, fed
    by either worktree file bytes or archive bytes. The archived state.json and
    quest.json are schema-identical to the in-flight ones (both written by
    State.save / Quest.create), so the two sections can't silently drift.
    Returns None on malformed/incomplete JSON so a single corrupt entry can't
    crash the whole `imp list` listing (callers skip None)."""
    try:
        st = json.loads(state_json)
        meta = json.loads(quest_json)
        return (quest_id, st["status"], st.get("step") or "-", last_activity,
                meta["title"], meta["created_at"], st.get("step_label"),
                st.get("phase_label"), st.get("started_at"))
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _row_for_id(control: Path, quest_id: str) -> _Row | None:
    from imp.engine.archive import read as archive_read
    from imp.engine.state import lock_is_live
    cdir = _quest_home(control, quest_id)
    live_home = (cdir / "quest.json").exists()
    # archive_read returns bytes | None; the live quest-home branch always finds
    # the files. Annotate so the None-guard below stays reachable (not dead code).
    state_json: bytes | None
    quest_json: bytes | None
    if live_home:
        # A quest home mid-creation has quest.json written but state.json not
        # yet (Quest.create is not atomic across the two files). The daemon's
        # scan loop can hit that window, so tolerate it like scan_homes
        # rather than crashing the whole listing.
        try:
            state_json = (cdir / "state" / "state.json").read_bytes()
            quest_json = (cdir / "quest.json").read_bytes()
        except FileNotFoundError:
            return None
        last = _last_event_iso(cdir / "events.jsonl") or ""
    else:
        state_json = archive_read.read_state_json(control, quest_id)
        quest_json = archive_read.read_quest_json(control, quest_id)
        if state_json is None or quest_json is None:
            return None
        last = _journal_last_iso(control, quest_id) or ""
    row = _status_row(quest_id, state_json, quest_json, last)
    if row is None:
        return None
    lock_live = lock_is_live(cdir / "lock")
    # Archived homes pass the stored status through unchanged and never consult the
    # halt-condition (readers stay single-version).
    halt_condition = json.loads(state_json).get("halt_condition") if live_home else None
    status = resolve_display_status(row[1], lock_live=lock_live, halt_condition=halt_condition)
    return (row[0], status, *row[2:])


def _status_rows(control: Path, scanned: list[HomeDigest] | None = None) -> list[_Row]:
    homes = make_store(control).scan_homes() if scanned is None else scanned
    rows: list[_Row] = []
    for h in homes:
        if h.state_json is None:
            continue
        if _is_backlog_home(h.meta, conversed=h.has_user_message,
                            lock_live=h.lock_live):
            continue  # un-materialized, never-conversed, unlocked seed -> Backlog
        row = _status_row(h.quest_id, h.state_json, h.quest_json, h.last_iso)
        if row is None:
            continue
        halt_condition = json.loads(h.state_json).get("halt_condition")
        status = resolve_display_status(
            row[1], lock_live=h.lock_live, halt_condition=halt_condition)
        row = (row[0], status, *row[2:])
        if row[1] not in _INFLIGHT:
            continue
        rows.append(row)
    return rows


def _backlog_rows(control: Path, scanned: list[HomeDigest] | None = None) -> list[_Row]:
    homes = make_store(control).scan_homes() if scanned is None else scanned
    rows: list[_Row] = []
    for h in homes:
        meta = h.meta
        if not _is_backlog_home(meta, conversed=h.has_user_message,
                                lock_live=h.lock_live):
            continue  # materialized / draft-origin / conversed / lock-live -> Active
        origin = meta.get("origin", "draft")
        ready = make_store(control).backlog_readiness(meta.get("depends_on", [])).ready
        created = meta.get("created_at", "")
        blocked = "" if ready else "blocked"
        rows.append((h.quest_id, origin, blocked, h.last_iso,
                     meta.get("title") or "", created, blocked or None, None, None))
    return rows


def _search_active(control: Path, query: str) -> list[SearchHit]:
    """In-flight + draft-origin quests whose id, title, or conversation prose
    contains `query` (case-insensitive)."""
    q = query.lower()
    scanned = make_store(control).scan_homes()
    prose_by_id = {d.quest_id: d.prose for d in scanned}
    hits: list[SearchHit] = []
    for r in _status_rows(control, scanned):
        prose = prose_by_id.get(r[0], "")
        if q in r[0].lower() or q in r[4].lower() or q in prose.lower():
            hits.append(SearchHit(r[0], r[4], "active", prose, r[3]))
    return hits


def _backlog_matches(control: Path, query: str) -> list[_BacklogMatch]:
    """Backlog quests whose id, title, or seed_opening body (title + description)
    contains `query` (case-insensitive)."""
    q = query.lower()
    scanned = make_store(control).scan_homes()
    seed_by_id = {d.quest_id: str(d.meta.get("seed_opening") or "") for d in scanned}
    out: list[_BacklogMatch] = []
    for r in _backlog_rows(control, scanned):
        quest_id, title = r[0], r[4]
        seed = seed_by_id.get(quest_id, "")
        if q in quest_id.lower() or q in title.lower() or q in seed.lower():
            out.append(_BacklogMatch(quest_id, title, seed, r[3]))
    return out


def _search_backlog(control: Path, query: str) -> list[SearchHit]:
    """Backlog quests whose id, title, or seed_opening body (title + description)
    contains `query` (case-insensitive)."""
    return [SearchHit(m.quest_id, m.title, "backlog", m.seed, m.last_activity)
            for m in _backlog_matches(control, query)]


def _search_archived(control: Path, query: str, statuses: set[str],
                     merged_after: str | None) -> list[SearchHit]:
    """Archive hits in `statuses`; completed hits also gated by merged_after."""
    from imp.engine.archive import read as archive_read
    hits: list[SearchHit] = []
    for cid, title, content, status, last in archive_read.search_archive_content(
            control, query):
        if status not in statuses:
            continue
        if (status == "merged" and merged_after is not None
                and not _merged_after(control, cid, merged_after)):
            continue
        hits.append(SearchHit(cid, title, "archive", content, last))
    return hits


def _merged_after(control: Path, quest_id: str, base_commit: str) -> bool:
    """True iff the recorded merge landed AFTER base_commit; fail-safe False otherwise."""
    from imp.engine import worktree
    from imp.engine.archive import read as archive_read
    merge_commit = archive_read.merge_commit_of(control, quest_id)
    if not merge_commit:
        return False
    # git exit 1 = not an ancestor (landed after) → keep; exit 0/any-other = exclude (fail-safe).
    return worktree._git(
        control, "merge-base", "--is-ancestor", merge_commit, base_commit,
    ).returncode == 1
