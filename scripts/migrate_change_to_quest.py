#!/usr/bin/env python3
"""One-off migration: rename imp's on-disk/global Change concept -> Quest.

Shipping code (Parts 1-2) already knows ONLY `quest`. This throwaway script
converts a developer's existing local .imp/ state to the new names. Run it ONCE,
from the control root (the dir that contains .imp/), while NO imp process is
running (so no in-flight run spans the cutover). It is not imported by any
product module.

Four stores are migrated, in dependency order:

  1. Homes        .imp/changes/ -> .imp/quests/ ; per-home change.json -> quest.json
  2. Records      .imp/archive/records/*.json    top-level change_meta -> quest_meta
  3. Merge queue  .git/imp/merge-queue.json       entry change_id -> quest_id
  4. Index        .imp/archive/index.db           deleted + rebuilt from records
                  under the new quests / quest_id schema (records are the source
                  of truth for the rebuild, so they are rewritten first).

Idempotent: a second run no-ops cleanly (skips the move when .imp/quests/ already
exists; records/merge-queue are already renamed). Use --dry-run to preview.

    python3 scripts/migrate_change_to_quest.py --dry-run
    python3 scripts/migrate_change_to_quest.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

# Make `imp` importable (archive primitives) when run from a checkout.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))


def migrate_homes(control_root: Path, dry_run: bool) -> int:
    """Move .imp/changes/ -> .imp/quests/ and rename each change.json ->
    quest.json. Returns the number of change.json files renamed. No-ops when
    .imp/quests/ already exists (already migrated)."""
    changes_dir = control_root / ".imp" / "changes"
    quests_dir = control_root / ".imp" / "quests"
    if quests_dir.exists():
        print("  [homes] .imp/quests/ already exists -- skipping move")
        return 0
    if not changes_dir.is_dir():
        print("  [homes] no .imp/changes/ -- nothing to move")
        return 0
    if dry_run:
        n = 0
        for meta in sorted(changes_dir.glob("*/change.json")):
            print(f"  [homes] would move + rename {meta.parent.name}/change.json"
                  " -> quest.json")
            n += 1
        return n
    shutil.move(str(changes_dir), str(quests_dir))
    renamed = 0
    for meta in sorted(quests_dir.glob("*/change.json")):
        meta.rename(meta.with_name("quest.json"))
        print(f"  [homes] {meta.parent.name}/change.json -> quest.json")
        renamed += 1
    return renamed


def migrate_records(control_root: Path, dry_run: bool) -> int:
    """Rewrite the top-level key change_meta -> quest_meta in every
    .imp/archive/records/*.json. Returns the number of records rewritten."""
    records_dir = control_root / ".imp" / "archive" / "records"
    if not records_dir.is_dir():
        return 0
    rewritten = 0
    for path in sorted(records_dir.glob("*.json")):
        rec = json.loads(path.read_text(encoding="utf-8"))
        if "change_meta" not in rec:
            continue
        rec["quest_meta"] = rec.pop("change_meta")
        rewritten += 1
        print(f"  [records] {path.name}: change_meta -> quest_meta")
        if not dry_run:
            path.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
    return rewritten


def migrate_merge_queue(control_root: Path, dry_run: bool) -> int:
    """Rewrite each entry's key change_id -> quest_id in
    .git/imp/merge-queue.json. Returns the number of entries rewritten.
    No-ops when the file is absent."""
    path = control_root / ".git" / "imp" / "merge-queue.json"
    if not path.exists():
        return 0
    entries = json.loads(path.read_text(encoding="utf-8"))
    rewritten = 0
    for entry in entries:
        if "change_id" in entry:
            entry["quest_id"] = entry.pop("change_id")
            rewritten += 1
    if rewritten:
        verb = "would rewrite" if dry_run else "rewrote"
        print(f"  [merge-queue] {verb} {rewritten} entry(ies) change_id -> quest_id")
        if not dry_run:
            path.write_text(json.dumps(entries, indent=2, sort_keys=True), encoding="utf-8")
    return rewritten


def rebuild_archive_index(control_root: Path, dry_run: bool) -> None:
    """Delete .imp/archive/index.db and rebuild it from the (now quest_meta)
    records under the new quests / quest_id schema."""
    archive_dir = control_root / ".imp" / "archive"
    if not archive_dir.is_dir():
        print("  [index] no .imp/archive/ -- nothing to rebuild")
        return
    print("  [index] delete index.db and rebuild under the quests schema")
    if dry_run:
        return
    from imp.engine.archive import rebuild_index

    (archive_dir / "index.db").unlink(missing_ok=True)
    rebuild_index(control_root)


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
    print(f"== migrate Change -> Quest ({mode}) under {root} ==")

    print("\n-- homes: .imp/changes/ -> .imp/quests/ --")
    migrate_homes(root, args.dry_run)
    print("\n-- archive records: change_meta -> quest_meta --")
    migrate_records(root, args.dry_run)
    print("\n-- merge queue: change_id -> quest_id --")
    migrate_merge_queue(root, args.dry_run)
    print("\n-- archive index: delete + rebuild --")
    rebuild_archive_index(root, args.dry_run)

    print("\n== done ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
