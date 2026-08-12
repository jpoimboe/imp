"""Session-daemon directory: liveness probes for per-quest session sockets."""
from __future__ import annotations

from pathlib import Path

from imp.remote.daemon.discover import socket_is_live
from imp.remote.session.discover import session_socket_path


def session_is_resident(control_root: Path, quest_id: str) -> bool:
    """True iff a session daemon is currently accepting on quest_id's socket."""
    return socket_is_live(session_socket_path(quest_id))


def resident_session_ids(control_root: Path) -> set[str]:
    """Quest ids with a live session socket; scans live homes only, never the archive."""
    from imp.cli.store import _list_quest_homes
    return {
        cid
        for cid in _list_quest_homes(control_root)
        if socket_is_live(session_socket_path(cid))
    }


def quest_home_exists(control_root: Path, quest_id: str) -> bool:
    """True iff quest_id has a home on disk under <state dir>/quests/ (not the archive)."""
    from imp.cli.store import _list_quest_homes
    return quest_id in _list_quest_homes(control_root)
