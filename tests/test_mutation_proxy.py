from __future__ import annotations

import json
from pathlib import Path

import pytest

import imp.remote.daemon.spawn as spawn_mod
from imp.cli.mutation_proxy import run_mutation
from imp.engine import worktree
from imp.engine.quest import Quest
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_run_mutation_resume_delegates_to_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def _fake_resume(control_root: Path, token: str) -> str:
        seen["control_root"] = control_root
        seen["token"] = token
        return "Resuming @questa…"

    monkeypatch.setattr(spawn_mod, "resume_quest", _fake_resume)
    result = run_mutation(tmp_path, "resume", {"token": "questa"})
    assert result == {"returncode": 0, "message": "Resuming @questa…"}
    assert seen == {"control_root": tmp_path, "token": "questa"}


def test_run_mutation_unknown_op_raises() -> None:
    with pytest.raises(ValueError):
        run_mutation(Path("/nope"), "explode", {})


def _draft(control: Path, quest_id: str) -> Quest:
    base = worktree.current_branch(control)
    return Quest.create_draft(
        control, title="Original", quest_id=quest_id, base_branch=base,
        origin="draft")


def _meta(control: Path, quest_id: str) -> dict:
    quest = Quest.load(control, quest_id)
    return json.loads((quest.quest_dir / "quest.json").read_text())


def test_run_mutation_edit_sets_depends_on(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _draft(tmp_path, "2026-06-24-mut10")
    result = run_mutation(tmp_path, "edit", {
        "token": "mut10", "title": None, "backlog": False, "active": False,
        "depends_on": ["dep77"], "clear_deps": False, "append_intent": None})
    assert result == {"returncode": 0, "message": ""}
    assert _meta(tmp_path, "2026-06-24-mut10")["depends_on"] == ["dep77"]


def test_run_mutation_edit_no_match_returns_error(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    result = run_mutation(tmp_path, "edit", {
        "token": "ghost", "title": "X", "backlog": False, "active": False,
        "depends_on": None, "clear_deps": False, "append_intent": None})
    assert result["returncode"] == 2
    assert "no quest matches" in result["message"]


def test_run_mutation_edit_ambiguous_returns_error(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _draft(tmp_path, "2026-06-24-ambig10")
    _draft(tmp_path, "2026-06-24-ambig20")
    result = run_mutation(tmp_path, "edit", {
        "token": "ambig", "title": "X", "backlog": False, "active": False,
        "depends_on": None, "clear_deps": False, "append_intent": None})
    assert result["returncode"] == 2
    assert "is ambiguous" in result["message"]


def test_run_mutation_new_records_backlog(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    result = run_mutation(tmp_path, "new", {
        "opening": "brief text", "title": None, "workflow": None, "base": None,
        "idea": "From the agent", "depends_on": None})
    assert result["returncode"] == 0
    assert "recorded backlog quest" in result["message"]
    quests = sorted((tmp_path / ".imp" / "quests").iterdir())
    assert Quest.load(tmp_path, quests[-1].name).title == "From the agent"
