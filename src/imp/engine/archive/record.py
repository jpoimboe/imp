"""Immutable per-Change record file: build, write, and read."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast


def build_record(
    change_meta: dict[str, Any], final_state: dict[str, Any], events_hash: str,
    *, resumable: bool = False,
) -> dict[str, Any]:
    return {
        "id": change_meta["id"],
        "title": change_meta["title"],
        "base": change_meta["base_branch"],
        "created_at": change_meta.get("created_at", ""),
        "depends_on": list(change_meta.get("depends_on") or []),
        "change_meta": dict(change_meta),
        "resumable": resumable,
        "final_state": final_state,
        "events_hash": events_hash,
    }


def write_record(records_dir: Path, record: dict[str, Any]) -> Path:
    records_dir.mkdir(parents=True, exist_ok=True)
    path = records_dir / f"{record['id']}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True))
    return path


def read_record(records_dir: Path, change_id: str) -> dict[str, Any] | None:
    path = records_dir / f"{change_id}.json"
    if not path.exists():
        return None
    return cast(dict[str, Any], json.loads(path.read_text()))


def list_record_ids(records_dir: Path) -> list[str]:
    if not records_dir.is_dir():
        return []
    return sorted(p.stem for p in records_dir.glob("*.json"))
