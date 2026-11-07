"""A resume token matching a quest on the active board ignores archived matches,
whatever the archive's version; a finished quest's leftover home shadows nothing."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import anyio
import pytest

import imp.cli.main as m
from imp.cli.store import LocalStore, _quest_home
from imp.engine import archive
from imp.engine.abandon import archive_conversation
from imp.engine.archive import archive_quest, record, records_root
from imp.engine.archive import index as archive_index
from imp.engine.errors import ArchiveMismatch
from imp.engine.quest import Quest
from imp.remote.daemon import spawn
from tests.conftest import BELOW_DATA_FLOOR, _seed_control

LIVE = "lodo40"
LIVE_TWO = "lodo41"
SHELVED = "lodo77"
SHELVED_TWO = "lodo88"
UNRELATED = "zeta01"


def _live(control: Path, quest_id: str = LIVE) -> None:
    Quest.create_draft(control, title=f"Live {quest_id}", quest_id=quest_id,
                       base_branch="main", origin="idea")


def _shelve(control: Path, quest_id: str = SHELVED) -> None:
    archive_conversation(Quest.create_draft(
        control, title=f"Shelved {quest_id}", quest_id=quest_id, base_branch="main"))


def _with_status(control: Path, quest_id: str, status: str) -> None:
    Quest.create(control, title=f"Quest {quest_id}", quest_id=quest_id,
                 base_branch="main")
    state_path = _quest_home(control, quest_id) / "state" / "state.json"
    state = json.loads(state_path.read_text())
    state["status"] = status
    state_path.write_text(json.dumps(state))


def _running(control: Path, quest_id: str) -> None:
    _with_status(control, quest_id, "paused")
    (_quest_home(control, quest_id) / "lock").write_text(str(os.getpid()))


def _archive_terminal(control: Path, quest_id: str) -> None:
    events = control / f"{quest_id}.jsonl"
    events.write_text("")
    quest_json = control / f"{quest_id}.quest.json"
    quest_json.write_text(json.dumps({
        "id": quest_id, "title": quest_id.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = control / f"{quest_id}.state.json"
    state_json.write_text(json.dumps(
        {"id": quest_id, "status": "completed", "step": "merge"}))
    archive_quest(control, quest_id, events_path=events,
                  quest_json_path=quest_json, state_json_path=state_json)


def _raise_data_floor(control: Path) -> int:
    ahead = archive._DATA_VERSION + 1
    archive.data_version_path(control).write_text(str(ahead))
    return ahead


def _skew_floor(control: Path, skew: str) -> None:
    if skew == "data-ahead":
        _raise_data_floor(control)
    elif skew == "data-below":
        archive._write_data_version(control, BELOW_DATA_FLOOR)
    else:
        conn = sqlite3.connect(archive.index_path(control))
        try:
            conn.execute(f"PRAGMA user_version = {archive_index._SCHEMA_VERSION + 1}")
            conn.commit()
        finally:
            conn.close()


def _resolve(control: Path, token: str, *, non_interactive: bool = True) -> str | int:
    async def run() -> str | int:
        return await m._resolve_resume_target(
            control, token, non_interactive=non_interactive)
    return anyio.run(run)


def _resolve_through_picker(
    control: Path, token: str, monkeypatch: pytest.MonkeyPatch,
) -> tuple[str | int, list[str]]:
    """Resolve on a fake tty, recording the ids the picker offers and choosing the first."""
    offered: list[str] = []

    async def fake_picker(candidates, *, now, color, header, selected_index):
        offered.extend(c.quest_id for c in candidates)
        return candidates[0].quest_id

    monkeypatch.setattr(m, "_interactive", lambda: True)
    monkeypatch.setattr("imp.cli.resume_select.run_picker", fake_picker)
    return _resolve(control, token, non_interactive=False), offered


def test_a_paused_live_match_resolves_with_no_picker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    _with_status(tmp_path, LIVE, "paused")
    _shelve(tmp_path)

    target, offered = _resolve_through_picker(tmp_path, "lodo", monkeypatch)

    assert target == LIVE
    assert offered == []


def test_two_live_matches_offer_only_the_live_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    _live(tmp_path, LIVE)
    _live(tmp_path, LIVE_TWO)
    _shelve(tmp_path)

    rows, unreadable = m._resumable_matches(tmp_path, "lodo")
    _, offered = _resolve_through_picker(tmp_path, "lodo", monkeypatch)

    assert sorted(r[0] for r in rows) == [LIVE, LIVE_TWO]
    assert unreadable is None
    assert sorted(offered) == [LIVE, LIVE_TWO]


def test_a_running_live_match_wins_over_an_archived_resumable_one(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _running(tmp_path, LIVE)
    _shelve(tmp_path)

    assert _resolve(tmp_path, "lodo") == LIVE


def test_no_live_match_resolves_a_lone_archived_resumable(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _live(tmp_path, UNRELATED)
    _shelve(tmp_path)

    assert _resolve(tmp_path, "lodo") == SHELVED


def test_no_live_match_offers_two_archived_resumables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    _live(tmp_path, UNRELATED)
    _shelve(tmp_path, SHELVED)
    _shelve(tmp_path, SHELVED_TWO)

    _, offered = _resolve_through_picker(tmp_path, "lodo", monkeypatch)

    assert sorted(offered) == [SHELVED, SHELVED_TWO]


def test_no_live_match_reaches_the_already_completed_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    _live(tmp_path, UNRELATED)
    _archive_terminal(tmp_path, SHELVED)

    assert _resolve(tmp_path, "lodo") == SHELVED
    assert m._load_or_rehydrate(tmp_path, SHELVED) == 0
    assert "already completed" in capsys.readouterr().out


def test_an_archived_full_id_resolves_despite_a_live_stem_match(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _live(tmp_path)
    _shelve(tmp_path)

    assert _resolve(tmp_path, SHELVED) == SHELVED


@pytest.mark.parametrize("skew", ["data-ahead", "data-below", "index-ahead"])
def test_floor_skew_cannot_change_a_live_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], skew: str,
) -> None:
    _seed_control(tmp_path)
    _live(tmp_path)
    _shelve(tmp_path)
    _skew_floor(tmp_path, skew)

    rows, unreadable = m._resumable_matches(tmp_path, "lodo")
    target = _resolve(tmp_path, "lodo")

    assert [r[0] for r in rows] == [LIVE]
    assert unreadable is None
    assert target == LIVE
    assert capsys.readouterr().err == ""


def test_a_leftover_home_does_not_hide_an_archived_resumable(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _with_status(tmp_path, LIVE, "completed")
    _shelve(tmp_path)

    assert _resolve(tmp_path, "lodo") == SHELVED


def test_a_leftover_home_does_not_shield_a_token_from_the_floor_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    _with_status(tmp_path, LIVE, "completed")
    _shelve(tmp_path)
    ahead = _raise_data_floor(tmp_path)
    reason = ArchiveMismatch(floor="data", stored=ahead, known=archive._DATA_VERSION)

    rc = _resolve(tmp_path, "lodo")

    assert rc == 2
    assert capsys.readouterr().err == (
        f"imp: 'lodo' matches an archived quest this imp cannot read: {SHELVED}\n"
        f"  {reason}\n")


@pytest.mark.parametrize(("status", "resumable"), [("completed", False), ("archived", True)])
def test_a_leftover_home_resolves_by_its_full_id_under_a_skewed_floor(
    tmp_path: Path, status: str, resumable: bool,
) -> None:
    _seed_control(tmp_path)
    _with_status(tmp_path, LIVE, status)
    _shelve(tmp_path)
    m._live_quest_ids(tmp_path)  # the stranded-home sweep gives the leftover a record too
    _raise_data_floor(tmp_path)
    rec = record.read_record(records_root(tmp_path), LIVE)
    assert rec is not None and bool(rec.get("resumable")) is resumable  # guard

    assert _resolve(tmp_path, LIVE) == LIVE


@pytest.mark.parametrize("token", ["lodo", "zzzz"])
def test_resumable_matches_scans_live_homes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str,
) -> None:
    """scan_homes parses every home's event log, and in-sandbox it is a daemon
    round-trip — one tokened lookup pays for it once, hit or miss."""
    _seed_control(tmp_path)
    _live(tmp_path)
    scans = {"n": 0}
    real = LocalStore.scan_homes

    def counted(self: LocalStore) -> list:
        scans["n"] += 1
        return real(self)

    monkeypatch.setattr(LocalStore, "scan_homes", counted)

    m._resumable_matches(tmp_path, token)

    assert scans["n"] == 1


def _two_live_and_one_archived(control: Path) -> None:
    _seed_control(control)
    _live(control, LIVE)
    _live(control, LIVE_TWO)
    _shelve(control)


def test_match_quest_ids_returns_only_the_live_hits(tmp_path: Path) -> None:
    _two_live_and_one_archived(tmp_path)

    assert sorted(m._match_quest_ids(tmp_path, "lodo")) == [LIVE, LIVE_TWO]


def test_match_quest_ids_reaches_an_archived_id_only_with_no_live_hit(tmp_path: Path) -> None:
    _two_live_and_one_archived(tmp_path)

    assert m._match_quest_ids(tmp_path, "lodo7") == [SHELVED]
    assert m._match_quest_ids(tmp_path, SHELVED) == [SHELVED]


def test_resolve_id_lists_only_the_live_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _two_live_and_one_archived(tmp_path)

    with pytest.raises(SystemExit) as exc:
        m._resolve_id(tmp_path, "lodo")

    err = capsys.readouterr().err
    assert exc.value.code == 2
    assert "'lodo' is ambiguous; candidates:" in err
    assert LIVE in err and LIVE_TWO in err
    assert SHELVED not in err


def test_remote_resume_names_only_the_live_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[list[str]] = []
    monkeypatch.setattr(spawn, "_spawn_detached", lambda argv, env, cwd: spawned.append(argv))
    _two_live_and_one_archived(tmp_path)

    msg = spawn.resume_quest(tmp_path, "lodo")

    assert msg.startswith("'lodo' is ambiguous: ")
    assert LIVE in msg and LIVE_TWO in msg
    assert SHELVED not in msg
    assert spawned == []


def test_two_running_matches_list_only_the_live_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_control(tmp_path)
    _running(tmp_path, LIVE)
    _running(tmp_path, LIVE_TWO)
    _shelve(tmp_path)

    rc = _resolve(tmp_path, "lodo")

    err = capsys.readouterr().err
    assert rc == 2
    assert "'lodo' is ambiguous; candidates:" in err
    assert LIVE in err and LIVE_TWO in err
    assert SHELVED not in err
