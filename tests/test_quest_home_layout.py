from pathlib import Path

from imp.engine.quest import Quest
from tests.conftest import _seed_control


def test_quest_home_and_meta_file_at_quests_path(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="X", quest_id="que123", base_branch="main")
    assert ch.quest_dir == tmp_path / ".imp" / "quests" / "que123"
    assert (ch.quest_dir / "quest.json").exists()
    assert not (ch.quest_dir / "change.json").exists()
    assert not (tmp_path / ".imp" / "changes").exists()


def test_artifacts_guard_admits_quests_path() -> None:
    from imp.agents.tools.limits import leads_to_or_within_artifacts, within_artifacts
    assert within_artifacts(Path(".imp/quests/que123/artifacts/plans/p.md"))
    assert leads_to_or_within_artifacts(Path(".imp/quests"))
    assert not within_artifacts(Path(".imp/changes/que123/artifacts/plans/p.md"))


def test_rehydrate_recreates_home_under_quests(tmp_path: Path) -> None:
    # Guards the pathlib-join site in read.py: rehydrate must place the scratch
    # home under .imp/quests (not .imp/changes) so a later Quest.load finds it.
    import json

    from imp.engine import archive
    from imp.engine.archive import read

    cid = "que777"
    events = tmp_path / f"{cid}.jsonl"
    events.write_text(
        f'{{"id":"{cid}-e0","timestamp":"2026-07-01T00:00:00+00:00",'
        f'"type":"change_created","payload":{{}}}}\n')
    quest_json = tmp_path / f"{cid}-quest.json"
    quest_json.write_text(json.dumps({
        "id": cid, "title": "Test Quest", "base_branch": "main",
        "created_at": "2026-07-01T00:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / f"{cid}-state.json"
    state_json.write_text(json.dumps({
        "id": cid, "status": "completed", "step": "finalize"}))
    archive.archive_quest(
        tmp_path, cid, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)

    result = read.rehydrate_quest(tmp_path, cid)

    assert result == tmp_path / ".imp" / "quests" / cid
    assert (tmp_path / ".imp" / "quests" / cid / "quest.json").exists()
    assert not (tmp_path / ".imp" / "changes").exists()
