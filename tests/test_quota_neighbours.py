from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from imp.cli.quota_neighbours import neighbour_slice, scan_neighbours
from imp.engine import archive
from tests.conftest import _archive_with_event

_START = "2026-06-01T03:30:00+00:00"
_END = "2026-06-01T05:38:00+00:00"


def _qr(ts: str, *, aid: str = "step:a", session: str = "s1",
        eid: str = "qr") -> dict:
    return {"id": eid, "agent_id": aid, "timestamp": ts, "type": "query_result",
            "payload": {"session_id": session, "duration_ms": 30000,
                        "total_cost_usd": 0.12, "usage": {}, "model_usage": {},
                        "num_turns": 3, "is_error": False}}


def _msg(ts: str, *, aid: str = "step:a", provider: str = "codex",
         model: str = "gpt-5.6-sol", auth: str = "chatgpt",
         eid: str = "am") -> dict:
    # `raw` is the verbatim provider response; its first block is the model's
    # thinking, which is what must never reach a neighbour's slice
    return {"id": eid, "agent_id": aid, "timestamp": ts, "type": "agent_message",
            "payload": {"provider": provider, "model": model, "auth_mode": auth,
                        "usage": {"input_tokens": 10, "output_tokens": 2},
                        "stop_reason": None,
                        "raw": {"content": [{"type": "thinking",
                                             "thinking": "SECRETPROSE"}]}}}


def _patch(*, aid: str = "step:a", tokens: int = 300) -> dict:
    return {"type": "agent_message_usage_patch", "agent_id": aid,
            "payload": {"usage": {"input_tokens": tokens, "output_tokens": 50,
                                  "cache_read_input_tokens": 600,
                                  "cache_creation_input_tokens": 100,
                                  "reasoning_output_tokens": 20}}}


def _reading(ts: str, *, used: float = 78.0, eid: str = "qk") -> dict:
    return {"id": eid, "agent_id": "step:a", "timestamp": ts,
            "type": "quota_reading",
            "payload": {"provider": "claude", "plan": None,
                        "windows": [{"label": "7d", "used_pct": used,
                                     "resets_at": 1788971433}], "raw": {}}}


def _prose(ts: str) -> dict:
    return {"id": "noise", "agent_id": "step:a", "timestamp": ts,
            "type": "tool_call",
            "payload": {"name": "bash", "input": {"cmd": "SECRETPROSE"}}}


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


def _live_home(pdir: Path, quest_id: str, events: list[dict]) -> Path:
    home = pdir / "quests" / quest_id
    (home / "state").mkdir(parents=True)
    (home / "quest.json").write_text(json.dumps({
        "id": quest_id, "title": quest_id, "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00"}))
    (home / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events))
    return home


def _jsonl(events: list[dict]) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


# ---------------------------------------------------------------- the slice


def test_slice_keeps_only_in_span_readings_and_usage() -> None:
    got = neighbour_slice("proj", "n1", [
        _reading("2026-06-01T03:00:00+00:00", eid="before"),
        _reading("2026-06-01T04:00:00+00:00", eid="inside"),
        _reading("2026-06-01T06:00:00+00:00", eid="after"),
        _prose("2026-06-01T04:00:01+00:00"),
        _qr("2026-06-01T04:10:00+00:00", eid="qr-in"),
        _qr("2026-06-01T06:10:00+00:00", eid="qr-after"),
    ], _START, _END)
    assert got is not None
    assert (got.project, got.quest_id) == ("proj", "n1")
    assert [r["id"] for r in got.readings] == ["inside"]
    assert [u["id"] for u in got.usage] == ["qr-in"]
    assert got.anchors == ()


def test_the_slice_carries_only_the_fields_the_units_pipeline_reads() -> None:
    got = neighbour_slice("proj", "n1", [
        _msg("2026-06-01T04:00:00+00:00", eid="msg-in"),
        _qr("2026-06-01T04:10:00+00:00", eid="qr-in"),
        _reading("2026-06-01T04:20:00+00:00", eid="qk-in"),
    ], _START, _END)
    assert got is not None
    msg, qr = got.usage
    assert sorted(msg["payload"]) == ["auth_mode", "model", "provider", "usage"]
    assert sorted(qr["payload"]) == [
        "duration_ms", "model_usage", "session_id", "total_cost_usd", "usage"]
    assert sorted(got.readings[0]["payload"]) == ["provider", "windows"]
    assert sorted(msg) == ["agent_id", "id", "payload", "timestamp", "type"]
    # the model's thinking text lives in payload.raw and must not cross the seam
    assert "SECRETPROSE" not in json.dumps(dataclasses.asdict(got))
    # what IS needed survives intact
    assert msg["payload"]["usage"] == {"input_tokens": 10, "output_tokens": 2}
    assert msg["payload"]["provider"] == "codex"


def test_the_anchor_is_the_last_pre_span_row_of_a_pair_that_runs_in_span() -> None:
    got = neighbour_slice("proj", "n1", [
        _qr("2026-06-01T03:00:00+00:00", eid="early", session="s1"),
        _qr("2026-06-01T03:20:00+00:00", eid="anchor", session="s1"),
        _qr("2026-06-01T03:40:00+00:00", eid="in", session="s1"),
        _qr("2026-06-01T03:25:00+00:00", eid="unused", session="s2"),
    ], _START, _END)
    assert got is not None
    assert [a["id"] for a in got.anchors] == ["anchor"]
    assert [u["id"] for u in got.usage] == ["in"]


def test_a_patch_row_takes_the_span_of_its_position_in_the_log() -> None:
    got = neighbour_slice("proj", "n1", [
        _msg("2026-06-01T03:00:00+00:00", aid="step:before", eid="msg-before"),
        _patch(aid="step:before", tokens=111),
        _msg("2026-06-01T04:00:00+00:00", aid="step:inside", eid="msg-inside"),
        _patch(aid="step:inside", tokens=222),
    ], _START, _END)
    assert got is not None
    assert [u.get("id", u["type"]) for u in got.usage] == [
        "msg-inside", "agent_message_usage_patch"]
    assert got.usage[1]["payload"]["usage"]["input_tokens"] == 222


def test_a_patch_is_in_span_though_the_message_it_patches_precedes_it() -> None:
    """AC 21(g): a codex turn's usage lands at `turn.completed`, which can fall
    inside the span while the `agent_message` carrying it fell before the start.
    The patch is the row that bills, so dropping it under-counts the neighbour."""
    got = neighbour_slice("proj", "n1", [
        _msg("2026-06-01T03:00:00+00:00", eid="msg-pre-span"),
        _prose("2026-06-01T04:00:00+00:00"),   # the turn's tool work, in span
        _patch(tokens=777),
    ], _START, _END)
    assert got is not None
    assert [u.get("id", u["type"]) for u in got.usage] == [
        "agent_message_usage_patch"]
    assert got.usage[0]["payload"]["usage"]["input_tokens"] == 777
    # priced from the pre-span message's triple, which the slice still resolves
    assert got.agents == {"step:a": ("codex", "gpt-5.6-sol", "chatgpt")}


def test_the_triple_is_the_last_message_at_or_before_the_span_end() -> None:
    got = neighbour_slice("proj", "n1", [
        _msg("2026-06-01T03:00:00+00:00", provider="claude",
             model="claude-opus-5", auth="subscription", eid="pre"),
        _qr("2026-06-01T04:00:00+00:00", eid="qr-in"),
        _msg("2026-06-01T06:00:00+00:00", provider="claude",
             model="claude-haiku-4-5", auth="subscription", eid="post"),
    ], _START, _END)
    assert got is not None
    assert got.agents == {"step:a": ("claude", "claude-opus-5", "subscription")}
    assert [u["id"] for u in got.usage] == ["qr-in"]


def test_a_neighbour_with_an_in_span_reading_and_no_usage_is_still_returned() -> None:
    got = neighbour_slice("proj", "n1", [
        _reading("2026-06-01T04:00:00+00:00", eid="inside"),
        _qr("2026-06-01T06:10:00+00:00", eid="qr-after"),
    ], _START, _END)
    assert got is not None
    assert [r["id"] for r in got.readings] == ["inside"]
    assert got.usage == ()


def test_a_quest_with_no_in_span_activity_yields_no_slice() -> None:
    assert neighbour_slice("proj", "n1", [
        _reading("2026-06-01T01:00:00+00:00"),
        _qr("2026-06-01T01:05:00+00:00"),
    ], _START, _END) is None


# ---------------------------------------------------------------- the walk


def _subject_project(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "subject"
    root.mkdir()
    _project(home, "subject", repo=root.resolve())
    return root


def test_neighbours_come_from_the_archive_and_live_homes_of_every_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _imp_home(tmp_path, monkeypatch)
    root = _subject_project(tmp_path, home)
    sdir = home / "projects" / "subject"
    _live_home(sdir, "samo71", [_reading("2026-06-01T03:31:00+00:00"),
                                _qr("2026-06-01T04:00:00+00:00")])
    _live_home(sdir, "sib01", [_qr("2026-06-01T04:05:00+00:00", aid="step:s")])
    other = tmp_path / "other"
    other.mkdir()
    _archive_with_event(other, "fego87", "Neighbour", _jsonl([
        _msg("2026-06-01T03:00:00+00:00"),
        _prose("2026-06-01T04:29:00+00:00"),
        _qr("2026-06-01T04:30:00+00:00")]))

    got = scan_neighbours(root, "samo71", _START, _END)

    assert {(n.project, n.quest_id) for n in got} == {
        ("subject", "sib01"), ("other", "fego87")}
    # the slice, not the log: the neighbour's tool_call prose never crosses
    assert "SECRETPROSE" not in json.dumps(
        [dataclasses.asdict(n) for n in got])


def test_the_subject_quest_is_not_its_own_neighbour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _imp_home(tmp_path, monkeypatch)
    root = _subject_project(tmp_path, home)
    events = [_qr("2026-06-01T04:00:00+00:00")]
    _live_home(home / "projects" / "subject", "samo71", events)
    # the same id, same log, in another project: identity is what excludes
    _live_home(_project(home, "elsewhere"), "samo71", events)

    got = scan_neighbours(root, "samo71", _START, _END)

    assert [(n.project, n.quest_id) for n in got] == [("elsewhere", "samo71")]


def test_a_quest_whose_activity_misses_the_span_is_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _imp_home(tmp_path, monkeypatch)
    root = _subject_project(tmp_path, home)
    pdir = _project(home, "elsewhere")
    _live_home(pdir, "inside", [_qr("2026-06-01T04:00:00+00:00")])
    _live_home(pdir, "before", [_qr("2026-06-01T01:00:00+00:00", eid="old")])
    _live_home(pdir, "after", [_qr("2026-06-01T09:00:00+00:00", eid="new")])

    got = scan_neighbours(root, "samo71", _START, _END)

    assert [n.quest_id for n in got] == ["inside"]


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
    (garbage / "archive" / "data_version").write_text(
        f"{archive._DATA_VERSION}\n")
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

    got = scan_neighbours(root, "samo71", _START, _END)

    assert [(n.project, n.quest_id) for n in got] == [("vanished", "ghost")]


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

    got = scan_neighbours(root, "samo71", _START, _END)

    assert [n.quest_id for n in got] == ["liveq"]


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

    got = scan_neighbours(root, "samo71", _START, _END)

    assert [(n.project, n.quest_id) for n in got] == [("other", "neigh1")]


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else b"<dir>")
            for p in sorted(root.rglob("*"))}


def test_the_walk_writes_nothing_in_another_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    got = scan_neighbours(root, "samo71", _START, _END)

    assert sorted(n.quest_id for n in got) == ["arch01", "neigh1", "neigh2"]
    assert _snapshot(odir) == before
    assert not (bare / "archive").exists()
