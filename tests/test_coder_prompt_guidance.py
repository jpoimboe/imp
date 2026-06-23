"""Bash-capable agents are given worktree-relative path guidance.

The relative-path guidance (originally inlined in coder.md) is now injected
by build_agent_invocation into ANY agent that holds the `bash` tool, so it can
never drift out of a prompt that author forgot to paste it into. This guards both
halves of that contract: the note carries the guidance, and the coder (a
bash holder) is therefore covered.
"""

from __future__ import annotations

from pathlib import Path

from imp.engine.agent_run import _BASH_VIRTUAL_PATHS_NOTE

CODER = (
    Path(__file__).resolve().parents[1]
    / "src" / "imp" / "assets" / "workflows" / "imp" / "agents" / "coder.md"
)


def test_bash_paths_note_carries_project_root_env() -> None:
    assert "$IMP_PROJECT_ROOT" in _BASH_VIRTUAL_PATHS_NOTE


def test_bash_paths_note_describes_relative_path_model() -> None:
    low = _BASH_VIRTUAL_PATHS_NOTE.lower()
    assert "relative" in low
    assert "project root" in low


def test_coder_holds_bash_so_it_receives_the_note() -> None:
    # The note reaches the coder because its frontmatter declares `bash`;
    # build_agent_invocation injects on tool presence (see test_agent_run.py).
    assert "bash" in CODER.read_text()


def test_bash_paths_note_drops_ls_and_describes_sandbox_bound() -> None:
    note = _BASH_VIRTUAL_PATHS_NOTE
    assert "`ls`" not in note                       # the stray ls tool mention is gone
    assert "sandbox" in note.lower()                # new out-of-worktree model named
    assert "relative to the project root" in note   # preserved for the prompt contract
    assert "grep" in note and "glob" in note     # nav tools named as worktree-relative
    assert "worktree" in note.lower() or "worktree-relative" in note.lower()
