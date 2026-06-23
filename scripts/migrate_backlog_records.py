"""One-time migration: convert legacy .imp/backlog/*.json records into
un-materialized change-homes, then delete the record directory.

Throwaway — run once per control root after the unified-backlog cutover; not part
of the product surface. Usage: `python scripts/migrate_backlog_records.py [CONTROL_ROOT]`
(defaults to the current working directory)."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from imp.engine import backlog, worktree
from imp.engine.change import Change


def migrate(control_root: Path) -> list[str]:
    backlog_dir = control_root / ".imp" / "backlog"
    if not backlog_dir.is_dir():
        return []
    base = worktree.current_branch(control_root)
    created: list[str] = []
    for path in sorted(backlog_dir.glob("*.json")):
        rec = json.loads(path.read_text())
        change_id = rec["id"]
        if (control_root / ".imp" / "changes" / change_id / "change.json").exists():
            continue  # already a change-home — idempotent
        origin = "followup" if str(rec.get("created_by", "")).startswith("step:") else "idea"
        Change.create_draft(
            control_root, title=rec["title"], change_id=change_id,
            base_branch=base, depends_on=list(rec.get("depends_on") or []),
            workflow=rec.get("workflow"), origin=origin,
            seed_opening=backlog.seed_opening(rec))
        created.append(change_id)
    shutil.rmtree(backlog_dir)
    return created


if __name__ == "__main__":
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    ids = migrate(root)
    print(f"migrated {len(ids)} backlog record(s): {', '.join(ids) or '(none)'}")
    print("removed .imp/backlog/")
