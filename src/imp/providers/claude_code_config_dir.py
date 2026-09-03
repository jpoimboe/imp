# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Ephemeral CLAUDE_CONFIG_DIR for fresh claude_code starts; caller cleans up."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from imp.engine.env import quest_scratch_root


def materialize_fresh_config_dir() -> Path:
    """Create a temp CLAUDE_CONFIG_DIR seeded with redacted auth; caller cleans up."""
    from claude_agent_sdk._internal.session_resume import _copy_auth_files

    tmp_base = Path(tempfile.mkdtemp(prefix="claude-fresh-", dir=str(quest_scratch_root())))
    try:
        (tmp_base / "projects").mkdir(parents=True, exist_ok=True)
        # {} → resolve the auth source from the real env, not the empty temp dir.
        _copy_auth_files(tmp_base, {})
    except BaseException:
        shutil.rmtree(tmp_base, ignore_errors=True)
        raise
    return tmp_base
