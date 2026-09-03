# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def session_socket_path(quest_id: str) -> Path:
    """Per-quest session socket at /tmp/imp-<uid>/<quest_id>.sock — a short, stable
    sibling of Quest.tmp_dir whose length is independent of project-root depth.
    Reachable at the same real path host-side and in-sandbox via the existing /tmp
    identity rw bind (sandbox.py bind_plan), so nothing is rewritten across the
    boundary. Real 6-char quest ids keep this far under the AF_UNIX sun_path limit,
    so no length guard is needed."""
    return Path(tempfile.gettempdir()) / f"imp-{os.getuid()}" / f"{quest_id}.sock"
