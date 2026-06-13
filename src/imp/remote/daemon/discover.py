from __future__ import annotations

import socket
from pathlib import Path


def socket_path(control_root: Path) -> Path:
    return control_root / ".imp" / "remote-control.sock"


def socket_is_live(path: Path | str) -> bool:
    """True iff a daemon is accepting on path; an absent path or unlistened socket is not live."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(path))
    except OSError:
        return False
    finally:
        probe.close()
    return True
