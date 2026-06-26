"""One-time migration: rewrite the legacy `wait_for_user` tool shape in historical
change data into the bare-turn-end shape (awaiting_user_input + user_message), so the
retained rendering/replay back-compat can be deleted.

Throwaway — run once per control root after the wait_for_user removal (imp change
zaba25). Usage: `python scripts/migrate_wfu_data.py [CONTROL_ROOT] [--apply]`
(defaults to the current working directory; dry-run unless --apply)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

Event = dict[str, Any]


def _is_wfu_call(e: Event) -> bool:
    return (e.get("type") == "tool_call"
            and (e.get("payload") or {}).get("name") == "wait_for_user")


def _wfu_call_ids(events: list[Event]) -> set[str | None]:
    return {(e.get("payload") or {}).get("id") for e in events if _is_wfu_call(e)}


def find_unsafe_events(events: list[Event]) -> list[str]:
    """wfu call ids that must NOT be auto-migrated: a resolved call with an error
    result, or with any tool_call/tool_result event between it and its result. A
    dangling call (no result) is safe and is not returned."""
    unsafe: list[str] = []
    positions = {(e.get("payload") or {}).get("id"): i
                 for i, e in enumerate(events) if _is_wfu_call(e)}
    for cid, ci in positions.items():
        ri = None
        for j in range(ci + 1, len(events)):
            p = events[j].get("payload") or {}
            if events[j].get("type") == "tool_result" and p.get("id") == cid:
                ri = j
                break
        if ri is None:
            continue  # dangling -> safe (handled by migrate_event_lines)
        if (events[ri].get("payload") or {}).get("error"):
            unsafe.append(cid)
            continue
        if any(events[k].get("type") in ("tool_call", "tool_result")
               for k in range(ci + 1, ri)):
            unsafe.append(cid)
    return unsafe


def migrate_event_lines(events: list[Event]) -> tuple[list[Event], int]:
    """Map each wfu tool_call -> awaiting_user_input and its paired tool_result ->
    user_message; a dangling call -> awaiting_user_input only. Returns (new_events,
    n_gates). Caller must ensure find_unsafe_events(events) is empty."""
    result_ids = _wfu_call_ids(events)
    out: list[Event] = []
    gates = 0
    for e in events:
        p = e.get("payload") or {}
        if _is_wfu_call(e):
            ne = dict(e)
            ne["type"] = "awaiting_user_input"
            ne["payload"] = {"prompt": ""}
            out.append(ne)
            gates += 1
        elif e.get("type") == "tool_result" and p.get("id") in result_ids:
            ne = dict(e)
            ne["type"] = "user_message"
            ne["payload"] = {"role": "user", "content": p.get("content") or []}
            out.append(ne)
        else:
            out.append(e)
    return out, gates


def _read_jsonl(path: Path) -> list[Event]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[Event]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def migrate_change_events_file(path: Path, *, apply: bool) -> int:
    new_events, gates = migrate_event_lines(_read_jsonl(path))
    if gates and apply:
        _write_jsonl(path, new_events)
    return gates


def _is_wfu_use(b: Any) -> bool:
    return (isinstance(b, dict) and b.get("type") == "tool_use"
            and b.get("name") == "wait_for_user")


def migrate_transcript_lines(msgs: list[Event]) -> tuple[list[Event], int]:
    """Drop wait_for_user tool_use blocks from assistant messages (dropping empty
    turns); rewrite tool_result blocks paired to wfu ids into the reply's text blocks.
    Returns (new_msgs, n_blocks_removed)."""
    wfu_ids = {b.get("id") for m in msgs if m.get("role") == "assistant"
               for b in (m.get("content") or []) if _is_wfu_use(b)}
    out: list[Event] = []
    removed = 0
    for m in msgs:
        role, content = m.get("role"), m.get("content") or []
        if role == "assistant" and any(_is_wfu_use(b) for b in content):
            kept = [b for b in content if not _is_wfu_use(b)]
            removed += sum(1 for b in content if _is_wfu_use(b))
            if not kept:
                # Dropping the empty turn is safe: no resumable transcript carries wfu
                # (all wfu transcripts are terminal/non-replayed), so the potential for
                # two consecutive user messages — which Anthropic replay rejects — is inert.
                continue
            nm = dict(m)
            nm["content"] = kept
            out.append(nm)
        elif role == "user" and any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            and (b.get("tool_use_id") or b.get("id")) in wfu_ids for b in content
        ):
            new_content: list[Any] = []
            for b in content:
                if (isinstance(b, dict) and b.get("type") == "tool_result"
                        and (b.get("tool_use_id") or b.get("id")) in wfu_ids):
                    new_content.extend(b.get("content") or [])
                else:
                    new_content.append(b)
            nm = dict(m)
            nm["content"] = new_content
            out.append(nm)
        else:
            out.append(m)
    return out, removed


def migrate_archive(control_root: Path, *, apply: bool) -> int:
    """For each archive record: rewrite its events blob and any transcript.jsonl
    step blobs via migrate_event_lines / migrate_transcript_lines; call
    rebuild_index once when anything changed. Returns number of records changed."""
    from imp.engine import archive
    from imp.engine.archive import record
    from imp.engine.archive.blobstore import LocalBlobStore

    store = LocalBlobStore(archive.blobs_root(control_root))
    recs_dir = archive.records_root(control_root)
    if not recs_dir.is_dir():
        return 0
    changed = 0
    for cid in record.list_record_ids(recs_dir):
        rec = record.read_record(recs_dir, cid)
        if rec is None:
            continue
        blob = store.get(rec["events_hash"])
        if blob is None:
            continue
        events = [json.loads(line) for line in blob.decode().splitlines() if line.strip()]
        new_events, gates = migrate_event_lines(events)

        step_blobs = dict(rec.get("step_blobs") or {})
        step_removed = 0
        for rel, digest in list(step_blobs.items()):
            if not rel.endswith("transcript.jsonl"):
                continue
            tb = store.get(digest)
            if tb is None:
                continue
            msgs = [json.loads(line) for line in tb.decode().splitlines() if line.strip()]
            new_msgs, n = migrate_transcript_lines(msgs)
            if n:
                step_removed += n
                if apply:
                    step_blobs[rel] = store.put(
                        "".join(json.dumps(m) + "\n" for m in new_msgs).encode())

        if gates or step_removed:
            changed += 1
            if apply:
                rec["events_hash"] = store.put(
                    "".join(json.dumps(e) + "\n" for e in new_events).encode())
                rec["step_blobs"] = step_blobs
                record.write_record(recs_dir, rec)
    if apply and changed:
        archive.rebuild_index(control_root)
    return changed


def _live_change_ids(control_root: Path) -> set[str]:
    """Change ids whose lock names a living pid — actively running, must not be
    rewritten under them. lock_is_live is false for a crashed change's stale lock."""
    from imp.engine.state import lock_is_live
    changes = control_root / ".imp" / "changes"
    if not changes.is_dir():
        return set()
    return {d.name for d in changes.iterdir()
            if d.is_dir() and lock_is_live(d / "lock")}


def collect_unsafe(control_root: Path, *, skip: set[str]) -> list[tuple[str, str]]:
    offenders: list[tuple[str, str]] = []
    for ev in (control_root / ".imp" / "changes").glob("*/events.jsonl"):
        if ev.parent.name in skip:
            continue
        for cid in find_unsafe_events(_read_jsonl(ev)):
            offenders.append((ev.parent.name, cid))
    from imp.engine import archive
    from imp.engine.archive import record
    from imp.engine.archive.blobstore import LocalBlobStore
    recs_dir = archive.records_root(control_root)
    if recs_dir.is_dir():
        store = LocalBlobStore(archive.blobs_root(control_root))
        for rid in record.list_record_ids(recs_dir):
            rec = record.read_record(recs_dir, rid)
            if rec is None:
                continue
            blob = store.get(rec["events_hash"])
            if blob is None:
                continue
            events = [json.loads(line) for line in blob.decode().splitlines() if line.strip()]
            for cid in find_unsafe_events(events):
                offenders.append((rid, cid))
    return offenders


def migrate(control_root: Path, *, apply: bool) -> dict[str, Any]:
    skip = _live_change_ids(control_root)
    offenders = collect_unsafe(control_root, skip=skip)
    if offenders and apply:
        raise SystemExit(
            f"refusing to migrate: {len(offenders)} wfu gate(s) need manual review "
            f"(interleaved or error result): {offenders}")
    report: dict[str, Any] = {"unsafe": offenders, "skipped_live": sorted(skip),
                               "live_events": 0, "live_transcripts": 0, "archive": 0}
    for ev in (control_root / ".imp" / "changes").glob("*/events.jsonl"):
        if ev.parent.name in skip:
            continue
        report["live_events"] += migrate_change_events_file(ev, apply=apply)
        report["live_transcripts"] += migrate_transcripts_under(
            ev.parent / "steps", apply=apply)
    report["archive"] = migrate_archive(control_root, apply=apply)
    return report


def migrate_transcripts_under(steps_dir: Path, *, apply: bool) -> int:
    """Walk steps_dir.rglob("transcript.jsonl") and rewrite each in place using
    migrate_transcript_lines. Returns total blocks removed. Dry-run when apply=False;
    missing dir returns 0."""
    if not steps_dir.is_dir():
        return 0
    total = 0
    for tpath in steps_dir.rglob("transcript.jsonl"):
        new_msgs, n = migrate_transcript_lines(_read_jsonl(tpath))
        if n:
            total += n
            if apply:
                _write_jsonl(tpath, new_msgs)
    return total


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate legacy wait_for_user data.")
    parser.add_argument("control_root", nargs="?", default=".", type=Path)
    parser.add_argument("--apply", action="store_true",
                        help="perform the migration (default: dry-run report only)")
    ns = parser.parse_args()
    rep = migrate(ns.control_root, apply=ns.apply)
    mode = "APPLIED" if ns.apply else "DRY-RUN"
    print(f"[{mode}] live_events={rep['live_events']} "
          f"live_transcripts={rep['live_transcripts']} archive_records={rep['archive']}")
    if rep["skipped_live"]:
        print(f"SKIPPED (running live — re-run when finished): {rep['skipped_live']}")
    if rep["unsafe"]:
        print(f"UNSAFE (needs manual review): {rep['unsafe']}")
