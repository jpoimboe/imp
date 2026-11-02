"""Cancelling the resume picker exits 130 with `imp: resume cancelled`, over a real terminal."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from imp.engine.quest import Quest
from impty import TerminalSession
from tests.impty.e2e_support import (
    E2E_EXIT_POLL_TIMEOUT,
    E2E_POLL_TIMEOUT,
    _poll,
    can_pty,
    clean_env,
    seed_minimal_repo,
)
from tests.impty.matchers import assert_screen

pytestmark = pytest.mark.skipif(not can_pty(), reason="pty not available")

_WORKTREE_SRC = str(Path(__file__).parent.parent.parent / "src")
_HEADER = "Resume which quest?"


def test_q_at_the_resume_picker_exits_130_cancelled(tmp_path: Path) -> None:
    seed_minimal_repo(tmp_path, script={"default": []})
    # Two fresh drafts are resumable (status `new`), so the picker opens.
    for qid, title in (("aaa111", "alpha quest"), ("bbb222", "beta quest")):
        Quest.create_draft(tmp_path, title=title, quest_id=qid, base_branch="main")
    env = clean_env(
        IMP_THEME="ansi",
        PYTHONPATH=_WORKTREE_SRC
        + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
    )
    with TerminalSession(
        [sys.executable, "-m", "imp", "resume"],
        cwd=str(tmp_path), env=env, cols=100, rows=24,
    ) as s:
        assert _poll(lambda: _HEADER in "\n".join(s.lines()), timeout=E2E_POLL_TIMEOUT), (
            "picker never painted\n" + "\n".join(s.lines()))
        s.send("q")
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

        assert rc == 130, "\n".join(s.lines())
        assert_screen(s, contains=("imp: resume cancelled",), excludes=(_HEADER,))
