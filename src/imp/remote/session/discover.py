# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def session_socket_path(quest_id: str) -> Path:
    """Per-quest session socket at /tmp/imp-<uid>/<quest_id>.sock — a short, stable
    sibling of Quest.tmp_dir whose length is independent of project-root depth.
    The same real path works host-side and in-sandbox: the daemon binds the socket
    outside the namespace, and the sandbox masks the scratch root with a tmpfs and,
    when the daemon has bound it, re-exposes THIS quest's own socket read-only
    through it, so connect() reaches it and nothing is rewritten across the boundary.
    A run with no daemon carries no re-expose and reaches no socket. Real 6-char quest
    ids keep this far under the AF_UNIX sun_path limit, so no length guard is needed."""
    return Path(tempfile.gettempdir()) / f"imp-{os.getuid()}" / f"{quest_id}.sock"
