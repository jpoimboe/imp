#!/usr/bin/env python3
"""One-off migration: rename the stale model-facing spawn tool `task` ->
`sub_agent` across local, gitignored runtime stores.

Background: commit c17cf97fa renamed the dispatch tool task -> sub_agent
everywhere, including the `imp log` narrative reader (which now only recognizes
name=="sub_agent" as a subagent dispatch). Event logs written before that commit
record dispatches with name=="task", so rendering them produces
"task result ... matched no open fold" warnings. The rename only changed the
name string (input schema + result format unchanged), so renaming the data field
is a complete migration.

Three stores are migrated (all under .imp/, gitignored):

  1. Live event logs   .imp/changes/*/events.jsonl
  2. Archive           .imp/archive/ (content-addressed blobs + records + index)
  3. Live snapshots    .imp/changes/*/workflow/{agents,partials}/*.md

Idempotent: a second run finds nothing to change. Use --dry-run to preview.

Run from the control root (the dir that contains .imp/), e.g.:

    python3 scripts/migrate_task_to_sub_agent.py --dry-run
    python3 scripts/migrate_task_to_sub_agent.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

# Make `imp` importable (archive primitives) when run from a checkout.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# Shared event-log transform (Stores 1 and 2)
# ---------------------------------------------------------------------------

def migrate_events_bytes(raw: bytes) -> tuple[bytes, int]:
    """Rename tool_call payload.name "task" -> "sub_agent" in JSONL events.

    Returns (new_bytes, n_changed). Lines that do not parse as JSON, or that are
    not a name=="task" tool_call, pass through byte-for-byte. n_changed counts
    rewritten events.
    """
    if b'"name": "task"' not in raw:
        return raw, 0
    out_lines: list[str] = []
    n_changed = 0
    # splitlines(keepends) preserves the original newline style per line; a final
    # line without a trailing newline is preserved as-is.
    for line in raw.decode("utf-8").splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            out_lines.append(line)
            continue
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            out_lines.append(line)  # never corrupt a malformed line
            continue
        payload = ev.get("payload")
        if (ev.get("type") == "tool_call"
                and isinstance(payload, dict)
                and payload.get("name") == "task"):
            payload["name"] = "sub_agent"
            n_changed += 1
            newline = "\n" if line.endswith("\n") else ""
            out_lines.append(json.dumps(ev) + newline)
        else:
            out_lines.append(line)
    return "".join(out_lines).encode("utf-8"), n_changed


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def locked_change_ids(control_root: Path) -> set[str]:
    """Change ids whose live runner holds the run lock (a living pid in
    <change_dir>/lock). Such changes are actively writing their own
    events.jsonl / executing from their snapshot, so the live stores (1 and 3)
    MUST NOT touch them. Stale lock files (dead pid) are not skipped."""
    from imp.engine.state import lock_is_live

    locked: set[str] = set()
    changes_dir = control_root / ".imp" / "changes"
    if not changes_dir.is_dir():
        return locked
    for d in sorted(changes_dir.iterdir()):
        if d.is_dir() and lock_is_live(d / "lock"):
            locked.add(d.name)
    return locked


# ---------------------------------------------------------------------------
# Store 1 — live event logs
# ---------------------------------------------------------------------------

def migrate_live_logs(
    control_root: Path, dry_run: bool, locked: set[str],
) -> tuple[int, int]:
    """Returns (files_changed, events_changed). Run-locked changes are skipped."""
    files_changed = events_changed = 0
    for log in sorted((control_root / ".imp" / "changes").glob("*/events.jsonl")):
        cid = log.parent.name
        if cid in locked:
            print(f"  [events] SKIP run-locked change {cid}")
            continue
        raw = log.read_bytes()
        new, n = migrate_events_bytes(raw)
        if n == 0:
            continue
        files_changed += 1
        events_changed += n
        print(f"  [events] {log.relative_to(control_root)}: {n} dispatch(es)")
        if not dry_run:
            _atomic_write(log, new)
    return files_changed, events_changed


# ---------------------------------------------------------------------------
# Store 2 — archive (blobs + records + derived index)
# ---------------------------------------------------------------------------

def migrate_archive(control_root: Path, dry_run: bool) -> tuple[int, int]:
    """Returns (records_changed, events_changed). Re-puts migrated event bytes
    under a new content hash, updates each record's events_hash, and refreshes
    the derived index per changed change."""
    from imp.engine import archive
    from imp.engine.archive import index, record
    from imp.engine.archive.blobstore import LocalBlobStore

    records_dir = archive.records_root(control_root)
    if not records_dir.is_dir():
        return 0, 0
    store = LocalBlobStore(archive.blobs_root(control_root))

    records_changed = events_changed = 0
    conn = None if dry_run else index.connect(archive.index_path(control_root))
    try:
        for cid in record.list_record_ids(records_dir):
            rec = record.read_record(records_dir, cid)
            if rec is None:
                continue
            blob = store.get(rec["events_hash"])
            if blob is None:
                continue
            new_bytes, n = migrate_events_bytes(blob)
            if n == 0:
                continue
            records_changed += 1
            events_changed += n
            print(f"  [archive] {cid}: {n} dispatch(es)")
            if dry_run:
                continue
            new_hash = store.put(new_bytes)
            rec["events_hash"] = new_hash
            record.write_record(records_dir, rec)
            index.ingest(conn, rec, archive._parse_events(new_bytes))
    finally:
        if conn is not None:
            conn.close()
    return records_changed, events_changed


# ---------------------------------------------------------------------------
# Store 3 — live workflow snapshots (prose / call-examples)
# ---------------------------------------------------------------------------

# Targeted, provably-safe substitutions. NOT a blanket task->sub_agent replace:
# "task" is overloaded in these prompts (## Task <N>, task_description,
# task-lead). `task(` is always the tool call; the backtick phrase is the patent
# workflow's generic-tool reference.
_SNAPSHOT_SUBS = (
    ("task(", "sub_agent("),
    ("`task` tool", "`sub_agent` tool"),
)


def migrate_snapshots(
    control_root: Path, dry_run: bool, locked: set[str],
) -> tuple[int, int]:
    """Returns (files_changed, total_substitutions). Run-locked changes skipped."""
    files_changed = subs_total = 0
    changes_dir = control_root / ".imp" / "changes"
    skipped_locked: set[str] = set()
    for md in sorted(changes_dir.glob("*/workflow/**/*.md")):
        # .imp/changes/<id>/workflow/... -> id is the part after "changes"
        rel = md.relative_to(changes_dir)
        cid = rel.parts[0]
        if cid in locked:
            if cid not in skipped_locked:
                skipped_locked.add(cid)
                print(f"  [snapshot] SKIP run-locked change {cid}")
            continue
        text = md.read_text(encoding="utf-8")
        new = text
        n = 0
        for old, repl in _SNAPSHOT_SUBS:
            n += new.count(old)
            new = new.replace(old, repl)
        if n == 0 or new == text:
            continue
        files_changed += 1
        subs_total += n
        print(f"  [snapshot] {md.relative_to(control_root)}: {n} substitution(s)")
        if not dry_run:
            _atomic_write(md, new.encode("utf-8"))
    return files_changed, subs_total


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--control-root", default=".", type=Path,
                    help="dir containing .imp/ (default: cwd)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    args = ap.parse_args()
    root = args.control_root.resolve()
    if not (root / ".imp").is_dir():
        print(f"error: no .imp/ under {root}", file=sys.stderr)
        return 2

    mode = "DRY RUN" if args.dry_run else "APPLY"
    print(f"== migrate task -> sub_agent ({mode}) under {root} ==")

    locked = locked_change_ids(root)
    if locked:
        print(f"run-locked changes (live stores skip these): {sorted(locked)}")

    print("\n-- Store 1: live event logs --")
    f1, e1 = migrate_live_logs(root, args.dry_run, locked)
    print(f"   {f1} file(s), {e1} dispatch(es)")

    print("\n-- Store 2: archive blobs + records + index --")
    f2, e2 = migrate_archive(root, args.dry_run)
    print(f"   {f2} record(s), {e2} dispatch(es)")

    print("\n-- Store 3: live workflow snapshots --")
    f3, s3 = migrate_snapshots(root, args.dry_run, locked)
    print(f"   {f3} file(s), {s3} substitution(s)")

    print("\n== done ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
