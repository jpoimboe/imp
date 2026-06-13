"""Contract for the autosquash-resolve agent prompt: it must name its terminal
complete_step, point at the tree oracle, instruct to drive the rebase to completion,
and forbid aborting/skipping (which would fail the verify gate)."""
from __future__ import annotations

from pathlib import Path

PROMPT = Path("src/imp/assets/workflows/default/agents/autosquash-resolve.md")


def test_autosquash_resolve_prompt_contract() -> None:
    text = PROMPT.read_text()
    low = text.lower()
    assert "complete_step" in text                          # terminal contract
    assert "autosquash-before-tree" in text                 # names the oracle sentinel
    assert "rebase --continue" in low                       # drive to completion
    assert "tree-preserving" in low                         # the hard constraint
    assert "do not" in low and "--abort" in text            # forbids aborting the fold
    assert "--skip" in text                                 # forbids skipping a commit
    assert "GIT_EDITOR=true" in text                        # must pin editor or wedge in editor-less envs
    assert "autosquash-before-head" in text                 # names the per-path content oracle
    assert "Error" in text and "cannot" in low              # honest error reporting: status Error when unable
