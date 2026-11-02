from __future__ import annotations

from pathlib import Path

import pytest

from imp.cli.abandon_confirm import abandon_question, removal_description
from imp.engine.quest import Quest
from tests.conftest import _commit_work, _seed_control


@pytest.mark.parametrize(("n", "phrase"), [
    (0, "no commits"), (1, "1 commit"), (3, "3 commits"),
])
def test_removal_description_counts_commits_ahead_of_base(
    tmp_path: Path, n: int, phrase: str,
) -> None:
    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    _commit_work(quest, n)
    assert removal_description(quest) == f"worktree and branch imp/feat ({phrase})"


def test_removal_description_is_none_for_a_draft(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    draft = Quest.create_draft(tmp_path, title="An idea", quest_id="idea1",
                               base_branch="main", origin="idea",
                               seed_opening="An idea\n\ndo a thing")
    assert removal_description(draft) is None


def test_abandon_question_matches_pinned_copy() -> None:
    assert abandon_question(
        "feat01", "worktree and branch imp/feat01 (5 commits)",
    ) == ("Abandon feat01? This removes its worktree and branch imp/feat01 "
          "(5 commits). [y/N] ")
