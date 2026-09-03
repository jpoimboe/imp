# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Archive maintenance: reap leaked quest homes already durable in the archive."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from imp.engine.archive import ensure_current, index, index_path
from imp.paths import project_state_dir


def reap_archived_homes(control_root: Path) -> int:
    """Reap live quest homes already terminal and durable in the archive; return the count."""
    from imp.cli.store import _INFLIGHT, _list_quest_homes, _quest_home

    ensure_current(control_root)
    db = index_path(control_root)
    if not db.exists():
        return 0
    conn = index.connect(db)
    try:
        reaped = 0
        for cid in _list_quest_homes(control_root):
            home = _quest_home(control_root, cid)
            try:
                state = json.loads((home / "state" / "state.json").read_bytes())
            except (FileNotFoundError, json.JSONDecodeError):
                continue
            if state.get("status") in _INFLIGHT:
                continue
            if not index.has_quest(conn, cid):
                continue
            shutil.rmtree(home, ignore_errors=True)
            reaped += 1
        return reaped
    finally:
        conn.close()


def reap_stray_homes(control_root: Path) -> int:
    """Remove empty stray quest-home dirs (no quest.json) under <state dir>/quests/;
    return the count."""
    quests_root = project_state_dir(control_root) / "quests"
    if not quests_root.is_dir():
        return 0
    reaped = 0
    for entry in quests_root.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if (entry / "quest.json").exists():
            continue
        try:
            os.rmdir(entry)
            reaped += 1
        except OSError:
            pass
    return reaped
