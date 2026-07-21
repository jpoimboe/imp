from __future__ import annotations

import os
from pathlib import Path

_SUN_PATH_MAX = 107  # AF_UNIX sun_path is 108 bytes incl. the NUL terminator


def session_socket_path(control_root: Path, quest_id: str) -> Path:
    """Per-quest session socket under the quest home
    (<control_root>/.imp/quests/<quest_id>/session.sock): reachable at the same real
    path host-side and in-sandbox via the quest-home rw bind, so no rewriting is
    needed. Fail fast (before any bind/connect) if the resolved path exceeds the
    AF_UNIX sun_path limit."""
    sock = control_root / ".imp" / "quests" / quest_id / "session.sock"
    encoded = os.fsencode(sock)
    if len(encoded) > _SUN_PATH_MAX:
        raise ValueError(
            f"session socket path is {len(encoded)} bytes, over the "
            f"{_SUN_PATH_MAX}-byte AF_UNIX sun_path limit: {sock}")
    return sock
