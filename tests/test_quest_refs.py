from __future__ import annotations

import json
from pathlib import Path

from imp.engine import archive, quest_refs
from imp.engine.quest import Quest
from imp.paths import project_state_dir
from tests.conftest import _seed_control


def _live_home(control: Path, qid: str, title: str) -> None:
    home = project_state_dir(control) / "quests" / qid
    home.mkdir(parents=True, exist_ok=True)
    (home / "quest.json").write_text(json.dumps(
        {"id": qid, "title": title, "base_branch": "main"}))


def _archive(control: Path, qid: str, title: str) -> None:
    """Archive a quest (no live home left) so is_archived is True and its title
    resolves from the archive record."""
    src = control / "_stage" / qid
    src.mkdir(parents=True)
    events = src / "events.jsonl"
    events.write_text(
        '{"id":"e0","parent_id":null,"agent_id":null,'
        '"timestamp":"2026-06-01T00:00:01+00:00","type":"quest_created",'
        f'"payload":{{"id":"{qid}"}}}}\n')
    quest_json = src / "quest.json"
    quest_json.write_text(json.dumps({
        "id": qid, "title": title, "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = src / "state.json"
    state_json.write_text(json.dumps({
        "id": qid, "status": "merged", "step": "finalize",
        "merge_commit": "abc123"}))
    archive.archive_quest(control, qid, events_path=events,
                          quest_json_path=quest_json, state_json_path=state_json)


def test_resolve_parent_none_returns_none(tmp_path: Path) -> None:
    assert quest_refs.resolve_parent(tmp_path, None) is None
    assert quest_refs.resolve_parent(tmp_path, "") is None


def test_resolve_parent_live_home_title(tmp_path: Path) -> None:
    _live_home(tmp_path, "prnt01", "Parent Quest")
    assert quest_refs.resolve_parent(tmp_path, "prnt01") == {
        "id": "prnt01", "title": "Parent Quest"}


def test_resolve_dependencies_titles_and_merged_flags(tmp_path: Path) -> None:
    _live_home(tmp_path, "livedp", "Live Dep")
    _archive(tmp_path, "mrgdp1", "Merged Dep")
    assert quest_refs.resolve_dependencies(tmp_path, ["livedp", "mrgdp1"]) == [
        {"id": "livedp", "title": "Live Dep", "merged": False},
        {"id": "mrgdp1", "title": "Merged Dep", "merged": True},
    ]
    assert quest_refs.resolve_dependencies(tmp_path, []) == []


def test_unresolvable_ref_falls_back_to_id(tmp_path: Path) -> None:
    assert quest_refs.resolve_parent(tmp_path, "ghost1") == {
        "id": "ghost1", "title": "ghost1"}
    assert quest_refs.resolve_dependencies(tmp_path, ["ghost2"]) == [
        {"id": "ghost2", "title": "ghost2", "merged": False}]


def test_resolve_for_quest_maps_the_right_fields(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _live_home(tmp_path, "prnt02", "The Parent")
    _archive(tmp_path, "mrgdp2", "Merged Dep2")
    q = Quest.create_draft(
        tmp_path, title="Child", quest_id="chld02", base_branch="main",
        parent="prnt02", depends_on=["mrgdp2"], defer_seed=True)
    parent_ref, deps = quest_refs.resolve_for_quest(q)
    assert parent_ref == {"id": "prnt02", "title": "The Parent"}
    assert deps == [{"id": "mrgdp2", "title": "Merged Dep2", "merged": True}]
