"""Contract for the autosquash-resolve agent prompt: it must name its terminal
complete_step, point at the tree oracle, instruct to drive the rebase to completion,
and forbid aborting/skipping (which would fail the verify gate)."""
from __future__ import annotations

import pytest
from pathlib import Path

PROMPT = Path("src/imp/assets/workflows/default/agents/autosquash-resolve.md")


@pytest.mark.parametrize("needle,case_fold", [
    ("complete_step", False),          # terminal contract
    ("autosquash-before-tree", False), # names the oracle sentinel
    ("rebase --continue", True),       # drive to completion
    ("tree-preserving", True),         # the hard constraint
    ("do not", True),                  # forbids aborting the fold
    ("--abort", False),                # forbids aborting the fold
    ("--skip", False),                 # forbids skipping a commit
    ("GIT_EDITOR=true", False),        # must pin editor or wedge in editor-less envs
    ("autosquash-before-head", False), # names the per-path content oracle
    ("Error", False),                  # honest error reporting: status Error when unable
    ("cannot", True),                  # honest error reporting: names inability
])
def test_autosquash_resolve_prompt_contract(needle: str, case_fold: bool) -> None:
    text = PROMPT.read_text()
    haystack = text.lower() if case_fold else text
    assert needle in haystack
