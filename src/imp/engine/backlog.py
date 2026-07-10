from __future__ import annotations

from pathlib import Path
from typing import Any

from imp.engine.archive import read as archive_read


def seed_opening(rec: dict[str, Any]) -> str:
    description = rec.get("description") or ""
    if description:
        return f"{rec['title']}\n\n{description}"
    return str(rec["title"])


def readiness(control_root: Path, depends_on: list[str]) -> tuple[bool, list[str]]:
    """Returns (ready, unmerged_ids). Ready iff every dep id is present in the
    archive (the index `quests` table)."""
    unmerged = [d for d in depends_on
                if not archive_read.is_archived(control_root, d)]
    return (not unmerged, unmerged)
