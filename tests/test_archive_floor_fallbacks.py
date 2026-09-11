"""A read that already declares a fallback for an absent record treats an
unreadable archive as absent; a read whose caller needs the record refuses."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import imp.cli.main as m
from imp.cli.main import main
from imp.engine import archive, quest_refs
from imp.engine.quest import Quest
from imp.paths import project_state_dir
from tests.conftest import _branch_with_commit, _seed_control

ARCH = "arqu99"
LIVE = "livid1"

_EVENTS = json.dumps({
    "id": "e0", "parent_id": None, "agent_id": None,
    "timestamp": "2026-06-01T00:00:01+00:00", "type": "step_started",
    "payload": {"step": "spec"}}) + "\n"


def _archive_merged(control: Path, quest_id: str, *,
                    merge_commit: str = "c0ffee") -> None:
    events = control / f"{quest_id}.jsonl"
    events.write_text(_EVENTS)
    quest_json = control / f"{quest_id}.quest.json"
    quest_json.write_text(json.dumps({
        "id": quest_id, "title": "Archived Dep", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": [],
        "materialized": True}))
    state_json = control / f"{quest_id}.state.json"
    state_json.write_text(json.dumps({
        "id": quest_id, "status": "merged", "step": None,
        "merge_commit": merge_commit}))
    archive.archive_quest(
        control, quest_id, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)


def _raise_data_floor(control: Path) -> int:
    """Stamp the blob floor one ahead — a newer imp's archive. Requires an index
    on disk, else _read_data_version reports this install's version."""
    ahead = archive._DATA_VERSION + 1
    archive.data_version_path(control).write_text(str(ahead))
    return ahead


def _live_quest_with_archived_dep(control: Path) -> Quest:
    _seed_control(control)
    _archive_merged(control, ARCH)
    quest = Quest.create(control, title="Live Thing", quest_id=LIVE,
                         base_branch="main", depends_on=[ARCH])
    _raise_data_floor(control)
    return quest


def test_title_falls_back_to_the_bare_id(tmp_path: Path) -> None:
    _live_quest_with_archived_dep(tmp_path)

    assert quest_refs._title(tmp_path, ARCH) == ARCH


def test_resolve_for_quest_assembles_prompt_context(tmp_path: Path) -> None:
    quest = _live_quest_with_archived_dep(tmp_path)

    parent, depends_on = quest_refs.resolve_for_quest(quest)

    assert parent is None
    assert depends_on == [{"id": ARCH, "title": ARCH, "merged": True}]


def test_show_renders_a_live_quest_with_an_archived_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _live_quest_with_archived_dep(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")

    rc = main(["show", LIVE])

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert "Live Thing" in captured.out
    assert f"depends_on:    {ARCH} · {ARCH} (merged)" in captured.out


def test_settled_outcome_returns_its_absent_record_outcome(tmp_path: Path) -> None:
    """A session teardown must not die on archive skew: no state.json and an
    unreadable archive is the same 'nothing to report' as no record at all."""
    from imp.cli.session_run import SettledOutcome, resolve_settled_outcome

    _seed_control(tmp_path)
    _archive_merged(tmp_path, ARCH)
    quest = Quest.create_draft(tmp_path, title="Live Thing", quest_id=LIVE,
                               base_branch="main")
    (quest.state_dir / "state.json").unlink()
    _raise_data_floor(tmp_path)

    assert resolve_settled_outcome(quest) == SettledOutcome(status=None, events=[])


def test_merged_after_returns_its_fail_safe_false(tmp_path: Path) -> None:
    """The fail-safe False, not the ordinary negative: this record's merge sha
    genuinely landed after the base, so the same call reads True while the
    archive is readable. Only the degrade can flip it."""
    from imp.cli.store import _merged_after
    from imp.engine import worktree

    _seed_control(tmp_path)
    landed_after = _branch_with_commit(tmp_path, "later")
    base = worktree.head_of(tmp_path, "main")
    _archive_merged(tmp_path, ARCH, merge_commit=landed_after)
    assert _merged_after(tmp_path, ARCH, base) is True     # guard: readable == True

    _raise_data_floor(tmp_path)

    assert _merged_after(tmp_path, ARCH, base) is False


def test_resume_offers_the_live_candidate_and_omits_the_unreadable_one(
    tmp_path: Path,
) -> None:
    """An archived candidate this install cannot read is simply not offered,
    exactly as an absent record is not — and `imp resume` still resolves."""
    import anyio

    from imp.engine.abandon import archive_conversation

    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="Live", quest_id="lodo40",
                       base_branch="main", origin="idea")
    shelved = Quest.create_draft(tmp_path, title="Shelved", quest_id="lodo77",
                                 base_branch="main")
    archive_conversation(shelved)
    assert not (project_state_dir(tmp_path) / "quests" / "lodo77").exists()
    _raise_data_floor(tmp_path)

    rows = m._resumable_match_rows(tmp_path, "lodo")

    async def _resolve() -> str | int:
        return await m._resolve_resume_target(
            tmp_path, "lodo", non_interactive=True)

    assert {r[0] for r in rows} == {"lodo40"}
    assert anyio.run(_resolve) == "lodo40"
