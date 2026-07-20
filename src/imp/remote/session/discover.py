from __future__ import annotations

import os
import tempfile
from pathlib import Path


def session_socket_path(control_root: Path, quest_id: str) -> Path:
    """Per-quest socket path; control_root kept for signature stability, unused."""
    return (
        Path(tempfile.gettempdir()) / f"imp-{os.getuid()}" / quest_id / "session.sock"
    )
