# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Ephemeral imp-owned CLAUDE_CONFIG_DIRs under the quest scratch root; caller cleans up."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, cast

from imp.engine.env import quest_scratch_root

if TYPE_CHECKING:
    from claude_agent_sdk import SessionStore

    from imp.providers.claude_session_store import ImpSessionStore

# The SDK's default ClaudeAgentOptions.load_timeout_ms; imp never overrides it.
_STORE_LOAD_TIMEOUT_S = 60.0


def make_config_dir(prefix: str) -> Path:
    """Create an empty 0700 config dir with projects/; caller cleans up."""
    base = Path(tempfile.mkdtemp(prefix=prefix, dir=str(quest_scratch_root())))
    try:
        (base / "projects").mkdir()
    except BaseException:
        shutil.rmtree(base, ignore_errors=True)
        raise
    return base


def materialize_fresh_config_dir() -> Path:
    """Create a temp CLAUDE_CONFIG_DIR seeded with redacted auth; caller cleans up."""
    from claude_agent_sdk._internal.session_resume import _copy_auth_files

    base = make_config_dir("claude-fresh-")
    try:
        # {} → resolve the auth source from the real env, not the empty temp dir.
        _copy_auth_files(base, {})
    except BaseException:
        shutil.rmtree(base, ignore_errors=True)
        raise
    return base


async def fill_resume_config_dir(
    config_dir: Path, store: ImpSessionStore, project_key: str, sid: str,
) -> None:
    """Seed redacted auth plus the store's session and subagent transcripts for --resume."""
    from claude_agent_sdk._internal.session_resume import (
        _copy_auth_files,
        _materialize_subkeys,
        _write_jsonl,
    )

    _copy_auth_files(config_dir, {})
    entries = await store.load({"project_key": project_key, "session_id": sid})
    assert entries is not None  # the caller overwrote a non-empty projection first
    project_dir = config_dir / "projects" / project_key
    _write_jsonl(project_dir / f"{sid}.jsonl", entries)
    await _materialize_subkeys(
        cast("SessionStore", store), config_dir, project_dir, project_key, sid,
        _STORE_LOAD_TIMEOUT_S)
