from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

# Linux AF_UNIX path limit is 108 bytes; pytest tmp dirs are long. Use a
# short hash-derived path in the system temp dir so the socket always fits.
_SOCK_DIGEST_SIZE = 12  # 24 hex chars — unique enough for one user's sessions


def session_socket_path(control_root: Path, quest_id: str) -> Path:
    key = f"{control_root.resolve()}:{quest_id}".encode()
    h = hashlib.blake2b(key, digest_size=_SOCK_DIGEST_SIZE).hexdigest()
    d = Path(tempfile.gettempdir()) / f"imp-{os.getuid()}"
    return d / f"{h}.sock"
