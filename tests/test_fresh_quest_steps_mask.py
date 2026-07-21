"""A fresh quest must create steps/ before policy bake so the --tmpfs steps/ mask is emitted."""
from __future__ import annotations

from pathlib import Path

from imp.engine.quest import Quest
from imp.engine.sandbox import SandboxConfig, resolve_policy
from tests.conftest import _seed_control


def test_fresh_quest_steps_dir_is_masked_by_resolved_policy(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    quest = Quest.create(
        tmp_path, title="x", quest_id="feat", base_branch="main",
    )
    # No step has run yet: steps/ must already exist so the mask is scheduled.
    assert (quest.quest_dir / "steps").is_dir()
    pol = resolve_policy(
        worktree=quest.project_root, control_root=tmp_path, quest_id="feat",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=quest.quest_dir,
    )
    assert str(quest.quest_dir / "steps") in pol.secret_dirs


def test_fresh_draft_creates_steps_dir(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    quest = Quest.create_draft(
        tmp_path, title="An idea", quest_id="idea1", base_branch="main",
    )
    assert (quest.quest_dir / "steps").is_dir()
