"""The week scan: every quest in every project whose facts can fall in the
subject's weeks, read without writing anything."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from imp.cli.quota_scan import other_logs, scan_weeks
from imp.cli.quota_share import parse_ts
from imp.engine import archive
from tests.conftest import _archive_with_event

_START = parse_ts("2026-06-01T03:30:00+00:00")
_END = parse_ts("2026-06-01T05:38:00+00:00")
_RESET = 1780642800                 # 2026-06-05T07:00Z, the week holding 2026-06-01


def _msg(ts: str, *, aid: str = "step:a") -> dict[str, Any]:
    return {"id": "am", "agent_id": aid, "timestamp": ts, "type": "agent_message",
            "payload": {"provider": "claude", "model": "claude-sonnet-4-6",
                        "auth_mode": "subscription"}}


def _qr(ts: str, *, aid: str = "step:a", session: str = "s1", eid: str = "qr",
        tokens: int = 0) -> dict[str, Any]:
    return {"id": eid, "agent_id": aid, "timestamp": ts, "type": "query_result",
            "payload": {"session_id": session, "duration_ms": 30000,
                        "model_usage": {"claude-sonnet-4-6": {"outputTokens": tokens}}}}


def _reading(ts: str, used: float) -> dict[str, Any]:
    return {"id": "qk", "agent_id": "step:a", "timestamp": ts, "type": "quota_reading",
            "payload": {"provider": "claude",
                        "windows": [{"label": "7d", "used_pct": used,
                                     "resets_at": _RESET}]}}


def _imp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "imphome"
    (home / "projects").mkdir(parents=True)
    monkeypatch.setenv("IMP_HOME", str(home))
    return home


def _project(home: Path, slug: str, repo: Path | None = None) -> Path:
    pdir = home / "projects" / slug
    pdir.mkdir(parents=True, exist_ok=True)
    if repo is not None:
        (pdir / "repo").write_text(f"{repo}\n")
    return pdir


def _live_home(pdir: Path, quest_id: str, events: list[dict[str, Any]]) -> Path:
    home = pdir / "quests" / quest_id
    (home / "state").mkdir(parents=True)
    (home / "quest.json").write_text(json.dumps({
        "id": quest_id, "title": quest_id, "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00"}))
    (home / "events.jsonl").write_text(_jsonl(events))
    return home


def _jsonl(events: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


def _subject_project(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "subject"
    root.mkdir()
    _project(home, "subject", repo=root.resolve())
    return root


def _found(root: Path) -> list[tuple[str, str]]:
    return [(p, q) for p, q, _events in other_logs(root, "samo71", _START, _END)]


def test_others_come_from_the_archive_and_live_homes_of_every_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _imp_home(tmp_path, monkeypatch)
    root = _subject_project(tmp_path, home)
    sdir = home / "projects" / "subject"
    _live_home(sdir, "samo71", [_qr("2026-06-01T04:00:00+00:00")])
    _live_home(sdir, "sib01", [_qr("2026-06-01T04:05:00+00:00", aid="step:s")])
    other = tmp_path / "other"
    other.mkdir()
    _archive_with_event(other, "fego87", "Neighbour",
                        _jsonl([_qr("2026-06-01T04:30:00+00:00")]))

    assert set(_found(root)) == {("subject", "sib01"), ("other", "fego87")}


def test_the_subject_is_skipped_only_in_its_own_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _imp_home(tmp_path, monkeypatch)
    root = _subject_project(tmp_path, home)
    events = [_qr("2026-06-01T04:00:00+00:00")]
    _live_home(home / "projects" / "subject", "samo71", events)
    _live_home(_project(home, "elsewhere"), "samo71", events)

    assert _found(root) == [("elsewhere", "samo71")]


def test_a_quest_whose_activity_misses_the_window_is_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _imp_home(tmp_path, monkeypatch)
    root = _subject_project(tmp_path, home)
    pdir = _project(home, "elsewhere")
    _live_home(pdir, "inside", [_qr("2026-06-01T04:00:00+00:00")])
    _live_home(pdir, "before", [_qr("2026-06-01T01:00:00+00:00", eid="old")])
    _live_home(pdir, "after", [_qr("2026-06-01T09:00:00+00:00", eid="new")])

    assert _found(root) == [("elsewhere", "inside")]


def test_a_degenerate_project_directory_is_skipped_and_the_rest_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _imp_home(tmp_path, monkeypatch)
    root = _subject_project(tmp_path, home)
    _project(home, "bare")                                   # no quests, no archive
    vanished = _project(home, "vanished", repo=tmp_path / "deleted-checkout")
    _live_home(vanished, "ghost", [_qr("2026-06-01T04:00:00+00:00")])
    garbage = _project(home, "garbage")
    (garbage / "archive").mkdir()
    (garbage / "archive" / "data_version").write_text(f"{archive._DATA_VERSION}\n")
    (garbage / "archive" / "index.db").write_bytes(b"not a database at all")
    notdirs = _project(home, "notdirs")
    (notdirs / "quests").write_text("a file where a directory belongs\n")
    (notdirs / "archive").write_text("likewise\n")
    dangling = _project(home, "dangling")
    (dangling / "quests").symlink_to(tmp_path / "moved-away")
    (dangling / "archive").symlink_to(tmp_path / "moved-away")
    badlog = _project(home, "badlog")
    _live_home(badlog, "unreadable", [_qr("2026-06-01T04:00:00+00:00")])
    (badlog / "quests" / "unreadable" / "events.jsonl").unlink()
    (badlog / "quests" / "unreadable" / "events.jsonl").mkdir()

    assert _found(root) == [("vanished", "ghost")]


def test_an_archive_this_install_cannot_read_is_skipped_but_its_homes_are_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _imp_home(tmp_path, monkeypatch)
    root = _subject_project(tmp_path, home)
    other = tmp_path / "other"
    other.mkdir()
    _archive_with_event(other, "archq", "Archived",
                        _jsonl([_qr("2026-06-01T04:30:00+00:00")]))
    odir = home / "projects" / "other"
    _live_home(odir, "liveq", [_qr("2026-06-01T04:40:00+00:00", eid="live")])
    (odir / "archive" / "data_version").write_text("1\n")

    assert _found(root) == [("other", "liveq")]


def test_a_quest_both_live_and_archived_in_one_project_is_returned_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _imp_home(tmp_path, monkeypatch)
    root = _subject_project(tmp_path, home)
    other = tmp_path / "other"
    other.mkdir()
    events = [_qr("2026-06-01T04:00:00+00:00")]
    # scan_homes archives a stranded terminal home and leaves it on disk, and
    # only a daemon for that project reaps it, so both copies stay readable
    _archive_with_event(other, "neigh1", "Both", _jsonl(events))
    _live_home(home / "projects" / "other", "neigh1", events)

    assert _found(root) == [("other", "neigh1")]


def test_the_peak_is_the_highest_reading_from_any_project_or_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 3(c): an archived quest's 50 beats another project's live 30 and the
    subject's own 10."""
    home = _imp_home(tmp_path, monkeypatch)
    root = _subject_project(tmp_path, home)
    subject = [_reading("2026-06-01T04:00:00+00:00", 10.0)]
    _live_home(home / "projects" / "subject", "samo71", subject)
    _live_home(_project(home, "elsewhere"), "liveq",
               [_reading("2026-06-01T04:10:00+00:00", 30.0)])
    other = tmp_path / "other"
    other.mkdir()
    _archive_with_event(other, "archq", "Archived",
                        _jsonl([_reading("2026-06-01T04:20:00+00:00", 50.0)]))

    (week,) = scan_weeks(root, "samo71", subject)
    assert (week.provider, week.resets_at, week.peak_pct) == ("claude", _RESET, 50.0)


def test_other_units_count_every_other_quest_once_and_never_the_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 3(d): 3.0 units from another project's live quest, 1.5 from an
    archived one, and 6.0 from one both live and archived (counted once). The
    subject's own 15.0 stay out of `other_units`."""
    home = _imp_home(tmp_path, monkeypatch)
    root = _subject_project(tmp_path, home)
    subject = [_msg("2026-06-01T03:59:00+00:00"), _reading("2026-06-01T04:00:00+00:00", 10.0),
               _qr("2026-06-01T04:01:00+00:00", tokens=1_000_000)]
    _live_home(home / "projects" / "subject", "samo71", subject)
    _live_home(_project(home, "elsewhere"), "liveq",
               [_msg("2026-06-01T04:09:00+00:00"),
                _qr("2026-06-01T04:10:00+00:00", tokens=200_000)])
    other = tmp_path / "other"
    other.mkdir()
    _archive_with_event(other, "archq", "Archived", _jsonl(
        [_msg("2026-06-01T04:19:00+00:00"),
         _qr("2026-06-01T04:20:00+00:00", tokens=100_000)]))
    both = [_msg("2026-06-01T04:29:00+00:00"),
            _qr("2026-06-01T04:30:00+00:00", tokens=400_000)]
    _archive_with_event(other, "bothq", "Both", _jsonl(both))
    _live_home(home / "projects" / "other", "bothq", both)

    (week,) = scan_weeks(root, "samo71", subject)
    assert round(week.other_units, 6) == 10.5


def test_a_neighbour_within_fourteen_days_of_the_subject_is_reached_and_one_beyond_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each neighbour's reading anchors its own week, so the weeks returned name
    exactly the neighbours the scan reached."""
    home = _imp_home(tmp_path, monkeypatch)
    root = _subject_project(tmp_path, home)
    subject = [_reading("2026-06-01T04:00:00+00:00", 10.0)]
    elsewhere = _project(home, "elsewhere")
    near_reset, far_reset = _RESET + 7 * 86400, _RESET + 21 * 86400
    for qid, ts, reset in (("near", "2026-06-11T04:00:00+00:00", near_reset),
                           ("far", "2026-06-21T04:00:00+00:00", far_reset)):
        ev = _reading(ts, 40.0)
        ev["payload"]["windows"][0]["resets_at"] = reset
        _live_home(elsewhere, qid, [ev])

    weeks = scan_weeks(root, "samo71", subject)
    assert [w.resets_at for w in weeks] == [_RESET, near_reset]


def test_a_subject_with_no_quota_facts_reads_no_other_quest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _imp_home(tmp_path, monkeypatch)
    root = _subject_project(tmp_path, home)
    _live_home(_project(home, "elsewhere"), "liveq",
               [_reading("2026-06-01T04:10:00+00:00", 30.0)])

    assert scan_weeks(root, "samo71", [{"type": "user_message",
                                        "timestamp": "2026-06-01T04:00:00+00:00"}]) == []


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else b"<dir>")
            for p in sorted(root.rglob("*"))}


def test_the_walk_writes_nothing_in_another_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 9(h)."""
    home = _imp_home(tmp_path, monkeypatch)
    root = _subject_project(tmp_path, home)
    # a REAL archive, so the walk runs past archive_is_current into the index
    # read and the blob read -- where the ensure_current / index.connect clauses
    # live; with no archive/ everything past that guard is unexercised
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _archive_with_event(elsewhere, "arch01", "Archived",
                        _jsonl([_qr("2026-06-01T04:10:00+00:00", eid="arch-qr")]))
    odir = home / "projects" / "elsewhere"
    _live_home(odir, "neigh1", [_qr("2026-06-01T04:00:00+00:00")])
    # a second project with no archive at all: index.connect() would create one
    bare = _project(home, "bare-project")
    _live_home(bare, "neigh2", [_qr("2026-06-01T04:20:00+00:00")])
    before = _snapshot(odir)

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("a write path was reached for a foreign project")

    # each module binds the name at import, so every binding the walk could
    # reach is patched separately -- patching imp.paths alone reaches none of them
    monkeypatch.setattr("imp.paths.project_state_dir", _boom)
    monkeypatch.setattr("imp.cli.store.project_state_dir", _boom)
    monkeypatch.setattr("imp.engine.archive.project_state_dir", _boom)
    monkeypatch.setattr("imp.engine.archive.read.project_state_dir", _boom)
    monkeypatch.setattr("imp.engine.archive.maintenance.project_state_dir", _boom)
    monkeypatch.setattr("imp.engine.archive.ensure_current", _boom)
    monkeypatch.setattr("imp.engine.archive.index.connect", _boom)
    monkeypatch.setattr("imp.engine.archive.archive_quest", _boom)
    monkeypatch.setattr("imp.engine.archive.maintenance.reap_archived_homes", _boom)

    assert sorted(q for _p, q in _found(root)) == ["arch01", "neigh1", "neigh2"]
    weeks = scan_weeks(root, "samo71", [_reading("2026-06-01T04:05:00+00:00", 7.0)])
    assert [w.peak_pct for w in weeks] == [7.0]
    assert _snapshot(odir) == before
    assert not (bare / "archive").exists()
