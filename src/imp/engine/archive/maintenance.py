"""Archive maintenance: reap leaked change homes already durable in the archive."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from imp.engine.archive import ensure_current, index, index_path


def reap_archived_homes(control_root: Path) -> int:
    """Reap live change homes already terminal and durable in the archive; return the count."""
    from imp.cli.main import _INFLIGHT, _quest_home, _list_quest_homes

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
