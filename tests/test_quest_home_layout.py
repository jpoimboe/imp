from pathlib import Path

import pytest

from imp.engine.quest import Quest
from imp.paths import project_state_dir
from tests.conftest import _seed_control


def test_artifacts_guard_admits_quests_path() -> None:
    from imp.agents.tools.limits import leads_to_or_within_artifacts, within_artifacts
    assert within_artifacts(Path(".imp/quests/que123/artifacts/plans/p.md"))
    assert leads_to_or_within_artifacts(Path(".imp/quests"))
    assert not within_artifacts(Path(".imp/changes/que123/artifacts/plans/p.md"))


def test_rehydrate_recreates_home_under_quests(tmp_path: Path) -> None:
    # Guards the pathlib-join site in read.py: rehydrate must place the scratch
    # home under quests/ (not changes/) so a later Quest.load finds it.
    import json

    from imp.engine import archive
    from imp.engine.archive import read

    cid = "que777"
    events = tmp_path / f"{cid}.jsonl"
    events.write_text(
        f'{{"id":"{cid}-e0","timestamp":"2026-07-01T00:00:00+00:00",'
        f'"type":"quest_created","payload":{{}}}}\n')
    quest_json = tmp_path / f"{cid}-quest.json"
    quest_json.write_text(json.dumps({
        "id": cid, "title": "Test Quest", "base_branch": "main",
        "created_at": "2026-07-01T00:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / f"{cid}-state.json"
    state_json.write_text(json.dumps({
        "id": cid, "status": "merged", "step": "finalize"}))
    archive.archive_quest(
        tmp_path, cid, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)

    result = read.rehydrate_quest(tmp_path, cid)

    state = project_state_dir(tmp_path)
    assert result == state / "quests" / cid
    assert (state / "quests" / cid / "quest.json").exists()
    assert not (state / "changes").exists()


def test_materialized_quest_lives_entirely_under_the_state_dir(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    state = project_state_dir(tmp_path)
    ch = Quest.create(tmp_path, title="X", quest_id="mat123", base_branch="main")
    assert ch.quest_dir == state / "quests" / "mat123"
    assert ch.project_root == (state / "worktrees" / "mat123").resolve()
    assert (ch.quest_dir / "quest.json").is_file()
    assert not (tmp_path / ".imp" / "quests").exists()
    assert not (tmp_path / ".imp" / "worktrees").exists()


def test_worktree_symlink_resolves_to_the_state_dir_home(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="X", quest_id="lnk123", base_branch="main")
    link = ch.project_root / ".imp" / "quests" / "lnk123"
    assert link.is_symlink()
    assert link.resolve() == ch.quest_dir.resolve()
    assert ".imp/" in (tmp_path / ".gitignore").read_text()


def test_a_draft_agent_writes_into_the_real_home_not_a_phantom(
    tmp_path: Path,
) -> None:
    # A draft has no worktree and no symlink, so the agent-facing home must be
    # absolute: the relative form would resolve against project_root (== the
    # control root) into a phantom .imp/quests/<id> inside the checkout.
    from imp.agents.tools.context import ToolContext

    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="X", quest_id="drf123",
                            base_branch="main")
    assert ch.project_root == tmp_path
    ctx = ToolContext(project_root=ch.project_root,
                      events_path=ch.events_path)
    written = ctx.resolve_path(f"{ch.agent_quest_home}/artifacts/spec/drf123.md")
    assert written == ch.quest_dir.resolve() / "artifacts" / "spec" / "drf123.md"
    assert not (tmp_path / ".imp" / "quests").exists()


def test_a_materialized_quest_keeps_the_worktree_relative_home(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="X", quest_id="mtz123", base_branch="main")
    assert ch.agent_quest_home == ".imp/quests/mtz123"
    # After a merge _exit_worktree points project_root back at the control root
    # while leaving materialized True — the relative form would be a phantom there.
    ch.set_project_root(ch.control_root)
    assert ch.agent_quest_home == str(ch.quest_dir)


def test_override_relocates_the_home_and_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    default_state = project_state_dir(tmp_path)
    override = tmp_path / "elsewhere" / "state"
    override.mkdir(parents=True)
    (override / "config.yaml").write_text(
        (default_state / "config.yaml").read_text())
    monkeypatch.setenv("IMP_STATE_DIR", str(override))
    ch = Quest.create(tmp_path, title="X", quest_id="ovr123", base_branch="main")
    assert ch.quest_dir == override / "quests" / "ovr123"
    assert ch.project_root == (override / "worktrees" / "ovr123").resolve()
    assert (ch.project_root / ".imp" / "quests" / "ovr123").resolve() == \
        ch.quest_dir.resolve()
    assert not (default_state / "quests").exists()
