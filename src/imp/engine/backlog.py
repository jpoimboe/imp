from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from imp.engine.archive import read as archive_read

if TYPE_CHECKING:
    from imp.engine.change import Change

def _dir(control_root: Path) -> Path:
    return control_root / ".imp" / "backlog"


def record_path(control_root: Path, change_id: str) -> Path:
    return _dir(control_root) / f"{change_id}.json"


def write_record(
    control_root: Path, *, change_id: str, title: str, description: str,
    depends_on: list[str], created_by: str,
) -> Path:
    if not title.strip():
        raise ValueError("backlog record title must be non-empty")
    d = _dir(control_root)
    d.mkdir(parents=True, exist_ok=True)
    path = record_path(control_root, change_id)
    path.write_text(json.dumps({
        "id": change_id, "title": title, "description": description,
        "depends_on": list(depends_on), "created_by": created_by,
        "created_at": datetime.now(UTC).isoformat(),
    }, indent=2, sort_keys=True))
    return path


def read_record(control_root: Path, change_id: str) -> dict[str, Any] | None:
    path = record_path(control_root, change_id)
    if not path.exists():
        return None
    return cast(dict[str, Any], json.loads(path.read_text()))


def seed_opening(rec: dict[str, Any]) -> str:
    description = rec.get("description") or ""
    if description:
        return f"{rec['title']}\n\n{description}"
    return str(rec["title"])


def list_ids(control_root: Path) -> list[str]:
    d = _dir(control_root)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def list_records(control_root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cid in list_ids(control_root):
        rec = read_record(control_root, cid)
        if rec is not None:
            out.append(rec)
    return out


def remove_record(control_root: Path, change_id: str) -> None:
    record_path(control_root, change_id).unlink(missing_ok=True)


def readiness(control_root: Path, depends_on: list[str]) -> tuple[bool, list[str]]:
    """Returns (ready, unmerged_ids). Ready iff every dep id is present in the
    archive (the index `changes` table, unioned with the legacy refs/imp/journal
    during the transition window)."""
    unmerged = [d for d in depends_on
                if not archive_read.is_archived(control_root, d)]
    return (not unmerged, unmerged)


def materialize_record(
    control_root: Path, change_id: str, *, base_branch: str,
) -> Change:
    # Runtime import avoids circular dependency (change.py imports nothing from backlog).
    from imp.engine.change import Change

    rec = read_record(control_root, change_id)
    if rec is None:
        raise FileNotFoundError(f"no backlog record {change_id!r}")
    change = Change.create(
        control_root, title=rec["title"], change_id=change_id,
        base_branch=base_branch, depends_on=rec["depends_on"])
    remove_record(control_root, change_id)
    return change
