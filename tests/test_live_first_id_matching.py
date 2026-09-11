"""Token resolution reaches a live quest without the archive.

Live homes match first and an unambiguous live hit short-circuits, so an archive
this install cannot read never blinds id resolution; a token that genuinely needs
the archive still raises."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import imp.cli.main as m
import imp.cli.store as s
from imp.cli.main import main
from imp.cli.mutation_proxy import run_mutation
from imp.cli.store import CompletedRow, LocalStore
from imp.engine import archive
from imp.engine.errors import ArchiveMismatch
from imp.engine.quest import Quest
from imp.paths import project_state_dir
from imp.remote.daemon import discover, spawn
from tests.conftest import _seed_control

LIVE = "livid1"

_EVENTS = json.dumps({
    "id": "e0", "parent_id": None, "agent_id": None,
    "timestamp": "2026-06-01T00:00:01+00:00", "type": "step_started",
    "payload": {"step": "spec"}}) + "\n"


class _UnreadableJournalStore(LocalStore):
    """An ahead-data-floor archive reduced to the one call the id space makes of it:
    live homes still scan, the journal drain raises."""

    def completed_rows(self) -> list[CompletedRow]:
        raise ArchiveMismatch(floor="data", stored=archive._DATA_VERSION + 1,
                              known=archive._DATA_VERSION)


def _live_quest_unreadable_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Live Thing", quest_id=LIVE, base_branch="main")
    monkeypatch.setattr(
        s, "make_store", lambda control: _UnreadableJournalStore(control))


def _archive_merged(control: Path, quest_id: str) -> None:
    events = control / f"{quest_id}.jsonl"
    events.write_text(_EVENTS)
    quest_json = control / f"{quest_id}.quest.json"
    quest_json.write_text(json.dumps({
        "id": quest_id, "title": "Archived Thing", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": [],
        "materialized": True}))
    state_json = control / f"{quest_id}.state.json"
    state_json.write_text(json.dumps({
        "id": quest_id, "status": "merged", "step": None, "merge_commit": "c0ffee"}))
    archive.archive_quest(
        control, quest_id, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)


def test_show_resolves_a_live_token_with_an_unreadable_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 2(a): the quest's data sits entirely in its own home, so the journal's
    state must not decide whether its id resolves."""
    _live_quest_unreadable_journal(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")

    rc = main(["show", "livid"])

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert "Live Thing" in captured.out


def test_a_token_matching_live_and_archived_ids_resolves_live(tmp_path: Path) -> None:
    """AC 3: a live/archived collision resolves live instead of exiting ambiguous,
    while the merged id space itself still enumerates both."""
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Live Thing", quest_id="lodo40", base_branch="main")
    _archive_merged(tmp_path, "lodo77")

    assert m._resolve_id(tmp_path, "lodo") == "lodo40"
    assert m._iter_quest_ids(tmp_path) == ["lodo40", "lodo77"]


def test_a_token_that_needs_the_archive_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No live hit means the token genuinely needs the merged id space: the refusal
    propagates, never collapsing into a false 'no quest matches'."""
    _live_quest_unreadable_journal(tmp_path, monkeypatch)

    with pytest.raises(ArchiveMismatch):
        m._resolve_id(tmp_path, "ghost9")


def test_matching_scans_live_homes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even on the fall-through path, where the live list is used twice. scan_homes
    reads and parses every home's events.jsonl and attempts to archive a stranded
    home, and in-sandbox it is a full daemon round-trip — so resolving an archived
    id must not pay for it twice."""
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Live Thing", quest_id="lodo40", base_branch="main")
    _archive_merged(tmp_path, "arch77")
    scans = {"n": 0}
    real = LocalStore.scan_homes

    def _counted(self: LocalStore) -> list:
        scans["n"] += 1
        return real(self)

    monkeypatch.setattr(LocalStore, "scan_homes", _counted)

    assert m._match_quest_ids(tmp_path, "arch") == ["arch77"]
    assert scans["n"] == 1


def test_edit_resolves_a_live_token_with_an_unreadable_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 2(b): the edit lands on the live quest, not an error frame."""
    _live_quest_unreadable_journal(tmp_path, monkeypatch)

    result = run_mutation(tmp_path, "edit", {
        "token": "livid", "title": "Renamed", "backlog": False, "active": False,
        "depends_on": None, "clear_deps": False, "append_intent": None,
        "base": None})

    assert result == {
        "returncode": 0, "message": 'edited livid1 · title → "Renamed"'}
    meta = json.loads(
        (project_state_dir(tmp_path) / "quests" / LIVE / "quest.json").read_text())
    assert meta["title"] == "Renamed"


def test_abandon_resolves_a_live_token_with_an_unreadable_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 2(c): the home is actually torn down, so the resolution really reached it."""
    _live_quest_unreadable_journal(tmp_path, monkeypatch)

    result = run_mutation(tmp_path, "abandon", {"token": "livid"})

    assert result == {"returncode": 0, "message": "Abandoned livid1."}
    assert not (project_state_dir(tmp_path) / "quests" / LIVE).exists()


def test_resume_quest_resolves_a_live_token_with_an_unreadable_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 2(d): `imp resume` is the remedy the ArchiveMismatch copy prescribes, and
    the remote/session path resolves its token through this seam."""
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        spawn, "_spawn_detached",
        lambda argv, env, cwd: spawned.append(argv))
    _live_quest_unreadable_journal(tmp_path, monkeypatch)

    msg = spawn.resume_quest(tmp_path, "livid")

    assert msg == "Resuming @livid1…"
    assert spawned == [[sys.executable, "-m", "imp", "resume", LIVE, "--headless"]]


def test_daemon_list_quests_reports_live_quests_with_an_unreadable_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 4: the daemon's poll enumerates live homes only — the raising store double
    is the proof it never drains the journal, and the live quest still surfaces."""
    _live_quest_unreadable_journal(tmp_path, monkeypatch)

    infos = discover.list_quests(tmp_path)

    assert [(i.id, i.title, i.status) for i in infos] == [(LIVE, "Live Thing", "new")]
