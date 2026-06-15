from __future__ import annotations

from pathlib import Path


def session_socket_path(control_root: Path, change_id: str) -> Path:
    return control_root / ".imp" / "sessions" / f"{change_id}.sock"
