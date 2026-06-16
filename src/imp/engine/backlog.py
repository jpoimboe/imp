from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _dir(control_root: Path) -> Path:
    return control_root / ".imp" / "backlog"


def record_path(control_root: Path, change_id: str) -> Path:
    return _dir(control_root) / f"{change_id}.json"


def write_record(
    control_root: Path, *, change_id: str, title: str, brief: str,
    depends_on: list[str], created_by: str,
) -> Path:
    d = _dir(control_root)
    d.mkdir(parents=True, exist_ok=True)
    path = record_path(control_root, change_id)
    path.write_text(json.dumps({
        "id": change_id, "title": title, "brief": brief,
        "depends_on": list(depends_on), "created_by": created_by,
        "created_at": datetime.now(UTC).isoformat(),
    }, indent=2, sort_keys=True))
    return path


def read_record(control_root: Path, change_id: str) -> dict[str, Any] | None:
    path = record_path(control_root, change_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def list_ids(control_root: Path) -> list[str]:
    d = _dir(control_root)
    if not d.is_dir():
        return []
    return [p.stem for p in d.glob("*.json")]


def list_records(control_root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cid in list_ids(control_root):
        rec = read_record(control_root, cid)
        if rec is not None:
            out.append(rec)
    return out


def remove_record(control_root: Path, change_id: str) -> None:
    record_path(control_root, change_id).unlink(missing_ok=True)
