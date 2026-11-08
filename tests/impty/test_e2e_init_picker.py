"""A fresh `imp init` over a real terminal: status-labelled picker rows, erased on confirm."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from impty import TerminalSession
from tests.impty.e2e_support import E2E_POLL_TIMEOUT, _git, _poll, can_pty, clean_env

pytestmark = pytest.mark.skipif(not can_pty(), reason="pty not available")

_WORKTREE_SRC = str(Path(__file__).parent.parent.parent / "src")
_HEADER = "Which providers do you want to enable?"
_HINT = "↑↓ move · space toggle · enter confirm · esc cancel"
_ROWS = [
    "> [x] claude      signed in",
    "  [ ] anthropic   ANTHROPIC_API_KEY not set",
    "  [ ] openai      OPENAI_API_KEY not set",
    "  [ ] codex       CLI not found",
]


def _init_env(tmp_path: Path) -> dict[str, str]:
    """claude on PATH and signed in, codex absent, no API keys: nothing read from the host."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = shutil.which("git")
    assert git is not None
    (bin_dir / "git").symlink_to(git)
    claude = bin_dir / "claude"
    claude.write_text("#!/bin/sh\nexit 0\n")
    claude.chmod(0o755)
    claude_dir = tmp_path / "claude-config"
    claude_dir.mkdir()
    (claude_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {}}))
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    env = clean_env(
        IMP_THEME="ansi",
        IMP_HOME=str(tmp_path / "imphome"),
        PATH=str(bin_dir),
        CLAUDE_CONFIG_DIR=str(claude_dir),
        CODEX_HOME=str(codex_home),
        PYTHONPATH=_WORKTREE_SRC
        + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
    )
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop(var, None)
    return env


def test_fresh_init_picker_shows_statuses_and_leaves_one_blank_line(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")

    with TerminalSession(
        [sys.executable, "-m", "imp", "init"],
        cwd=str(repo), env=_init_env(tmp_path), cols=100, rows=30,
    ) as s:
        assert _poll(lambda: any(_HINT in r for r in s.lines()), timeout=E2E_POLL_TIMEOUT), (
            "picker never painted\n" + "\n".join(s.lines()))
        rows = s.lines()
        for row in _ROWS:
            assert row in rows, "\n".join(rows)

        s.send("\r")
        assert _poll(lambda: any(r.startswith("Enabled: ") for r in s.lines()),
                     timeout=E2E_POLL_TIMEOUT), (
            "no summary after confirm\n" + "\n".join(s.lines()))

        rows = s.lines()
        screen = "\n".join(rows)
        assert not any(_HEADER in r or _HINT in r or "[x] " in r or "[ ] " in r
                       for r in rows), screen
        head = next(i for i, r in enumerate(rows) if r.startswith("Setting up imp in"))
        at = next(i for i, r in enumerate(rows) if r.startswith("Enabled: "))
        assert rows[at] == "Enabled: claude", screen
        assert rows[at - 1] == "", screen
        assert head < at - 1, screen
        assert "" not in rows[head:at - 1], screen
