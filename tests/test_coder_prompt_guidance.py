"""Bash-capable agents are given worktree-relative path guidance.

The relative-path guidance (originally inlined in implementer.md) is now injected
by build_agent_invocation into ANY agent that holds the `bash` tool, so it can
never drift out of a prompt that author forgot to paste it into. This guards both
halves of that contract: the note carries the guidance, and the implementer (a
bash holder) is therefore covered.
"""

from __future__ import annotations

from pathlib import Path

from imp.engine.agent_run import _BASH_VIRTUAL_PATHS_NOTE

IMPLEMENTER = (
    Path(__file__).resolve().parents[1]
    / "src" / "imp" / "assets" / "workflows" / "default" / "agents" / "implementer.md"
)


def test_bash_paths_note_carries_project_root_env() -> None:
    assert "$IMP_PROJECT_ROOT" in _BASH_VIRTUAL_PATHS_NOTE


def test_bash_paths_note_describes_relative_path_model() -> None:
    low = _BASH_VIRTUAL_PATHS_NOTE.lower()
    assert "relative" in low
    assert "project root" in low


def test_implementer_holds_bash_so_it_receives_the_note() -> None:
    # The note reaches the implementer because its frontmatter declares `bash`;
    # build_agent_invocation injects on tool presence (see test_agent_run.py).
    assert "bash" in IMPLEMENTER.read_text()
