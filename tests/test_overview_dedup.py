from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import imp.cli.main as m
from imp.engine import archive
from imp.engine.change import Change
from tests.conftest import _seed_control


def test_scan_change_homes_parses_each_home_once(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="A", quest_id="alpha", base_branch="main")
    Change.create(tmp_path, title="B", quest_id="beta", base_branch="main")
    ch_a = Change.load(tmp_path, "alpha")
    ch_a.events_path.write_text('{"timestamp":"2026-06-05T12:00:00+00:00"}\n')
    # Change.create seeds a timestamped change_created event; truncate beta's
    # events.jsonl to exercise the empty-file -> "" branch of _scan_change_homes.
    Change.load(tmp_path, "beta").events_path.write_text("")

    scan = {h.quest_id: h for h in m._scan_change_homes(tmp_path)}
    assert set(scan) == {"alpha", "beta"}
    assert scan["alpha"].meta["title"] == "A"
    assert scan["alpha"].state_json is not None
    assert scan["alpha"].last_iso == "2026-06-05T12:00:00+00:00"
    assert scan["beta"].last_iso == ""          # empty events.jsonl -> ""


def test_scan_change_homes_missing_state_json(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="No State", quest_id="noState", base_branch="main")
    state_file = tmp_path / ".imp" / "changes" / "noState" / "state" / "state.json"
    state_file.unlink()

    scan = {h.quest_id: h for h in m._scan_change_homes(tmp_path)}
    assert "noState" in scan
    assert scan["noState"].state_json is None


def test_scan_change_homes_drops_malformed_change_json(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="Bad JSON", quest_id="badJson", base_branch="main")
    change_file = tmp_path / ".imp" / "changes" / "badJson" / "change.json"
    change_file.write_bytes(b"bad json")

    ids = {h.quest_id for h in m._scan_change_homes(tmp_path)}
    assert "badJson" not in ids


def test_status_rows_uses_passed_scan(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="A", quest_id="alpha", base_branch="main")
    Change.create(tmp_path, title="B", quest_id="beta", base_branch="main")

    scan = m._scan_change_homes(tmp_path)
    # Threaded scan and internal scan produce identical rows.
    assert m._status_rows(tmp_path, scan) == m._status_rows(tmp_path)
    assert {r[0] for r in m._status_rows(tmp_path)} == {"alpha", "beta"}
    # The passed scan is actually honored, not ignored: a one-home scan yields one row.
    alpha_only = [h for h in scan if h.quest_id == "alpha"]
    assert {r[0] for r in m._status_rows(tmp_path, alpha_only)} == {"alpha"}


def test_backlog_rows_uses_passed_scan(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="Brief", quest_id="bk1",
                        base_branch="main", origin="idea", seed_opening="Brief")
    Change.create_draft(tmp_path, title="Brief2", quest_id="bk2",
                        base_branch="main", origin="idea", seed_opening="Brief2")

    scan = m._scan_change_homes(tmp_path)
    assert m._backlog_rows(tmp_path, scan) == m._backlog_rows(tmp_path)
    assert {r[0] for r in m._backlog_rows(tmp_path)} == {"bk1", "bk2"}
    bk1_only = [h for h in scan if h.quest_id == "bk1"]
    assert {r[0] for r in m._backlog_rows(tmp_path, bk1_only)} == {"bk1"}


def _archive_completed(control: Path, cid: str) -> None:
    events = control / f"{cid}.jsonl"
    events.write_bytes(
        f'{{"id":"{cid}-e0","parent_id":null,"agent_id":null,'
        '"timestamp":"2026-06-01T00:00:09+00:00","type":"step_started",'
        f'"payload":{{"id":"{cid}"}}}}\n'.encode())
    change_json = control / f"{cid}-change.json"
    change_json.write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": [],
        "materialized": True}))
    state_json = control / f"{cid}-state.json"
    state_json.write_text(json.dumps({
        "id": cid, "status": "completed", "step": None, "merge_commit": "c0ffee"}))
    archive.archive_change(
        control, cid, events_path=events,
        change_json_path=change_json, state_json_path=state_json)


def _archive_paused(
    control: Path, cid: str, *, last_ts: str = "2026-06-03T00:00:00+00:00"
) -> None:
    events = control / f"{cid}.jsonl"
    events.write_bytes(
        f'{{"id":"{cid}-e0","parent_id":null,"agent_id":null,'
        f'"timestamp":"{last_ts}","type":"step_started",'
        f'"payload":{{"id":"{cid}"}}}}\n'.encode())
    change_json = control / f"{cid}-change.json"
    change_json.write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-02T00:00:00+00:00", "depends_on": [],
        "materialized": False}))
    state_json = control / f"{cid}-state.json"
    state_json.write_text(json.dumps({
        "id": cid, "status": "paused", "step": None}))
    archive.archive_change(
        control, cid, events_path=events,
        change_json_path=change_json, state_json_path=state_json)


def test_completed_rows_honors_live_ids(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_completed(tmp_path, "gamma")
    assert {r[0] for r in m._completed_rows(tmp_path)} == {"gamma"}
    assert m._completed_rows(tmp_path, live_ids={"gamma"}) == []


def test_left_rows_selects_paused_not_live(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_paused(tmp_path, "delta")
    _archive_completed(tmp_path, "epsilon")
    # A left (archived `paused`) conversation is a left row; a completed one is
    # NOT (this pins the `paused` filter — dropping it would surface epsilon).
    left = m._left_rows(tmp_path)
    assert {r[0] for r in left} == {"delta"}
    assert all(r[1] == "paused" for r in left)
    # The completed change is a completed row, not a left row (AC 4).
    assert {r[0] for r in m._completed_rows(tmp_path)} == {"epsilon"}
    # A resumed (now live) left conversation drops out via live_ids (AC 3).
    assert m._left_rows(tmp_path, live_ids={"delta"}) == []


def test_left_conversation_only_in_all_history(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_completed(tmp_path, "gamma")          # merged, last=2026-06-01
    _archive_paused(tmp_path, "delta")             # left,   last=2026-06-03

    # AC 1: under --all, delta appears in history, sorted newest-first (before
    # the older merged gamma).
    all_hist = m._history_rows(tmp_path, show_all=True)
    assert [r[0] for r in all_hist] == ["delta", "gamma"]
    assert dict((r[0], r[1]) for r in all_hist)["delta"] == "paused"

    # AC 2: plain imp show never lists the left conversation.
    plain_hist = m._history_rows(tmp_path, show_all=False)
    assert {r[0] for r in plain_hist} == {"gamma"}

    # AC 3: a resumed (now live) left conversation is not duplicated in history.
    assert {r[0] for r in
            m._history_rows(tmp_path, show_all=True, live_ids={"delta"})} == {"gamma"}

    # AC 5: rendered overview tags it "left" under --all and omits it otherwise.
    now = datetime(2026, 6, 28, tzinfo=UTC)
    all_lines = m._overview_lines(tmp_path, show_all=True, now=now, color=False)
    assert any(ln.startswith("delta") and "left" in ln for ln in all_lines)
    plain_lines = m._overview_lines(tmp_path, show_all=False, now=now, color=False)
    assert not any(ln.startswith("delta") for ln in plain_lines)


def test_status_row_carries_phase_label() -> None:
    import json

    import imp.cli.main as m
    state = json.dumps({"status": "running", "step": "s", "step_label": "L",
                        "phase_label": "P"}).encode()
    change = json.dumps({"title": "T", "created_at": "2026-06-20T11:00:00+00:00"}).encode()
    row = m._status_row("cid", state, change, "2026-06-20T11:00:00+00:00")
    assert len(row) == 8
    assert row[7] == "P"


def test_completed_rows_pad_phase_label_slot(tmp_path: Path, monkeypatch) -> None:
    import imp.cli.main as m
    from tests.conftest import _seed_control
    _seed_control(tmp_path)
    monkeypatch.setattr(
        m.archive_read, "completed_rows",
        lambda control: [("cid", "completed", "-", "iso", "Title", "created", None)])
    monkeypatch.setattr(m, "_status_rows", lambda control: [])
    rows = m._completed_rows(tmp_path)
    assert rows == [("cid", "completed", "-", "iso", "Title", "created", None, None)]


def test_history_rows_threads_live_ids(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_completed(tmp_path, "gamma")
    assert {r[0] for r in m._history_rows(tmp_path, show_all=False)} == {"gamma"}
    assert m._history_rows(tmp_path, show_all=False, live_ids={"gamma"}) == []


def test_overview_lines_scans_homes_once(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="A", quest_id="alpha", base_branch="main")
    Change.create_draft(tmp_path, title="Brief", quest_id="bk1",
                        base_branch="main", origin="idea", seed_opening="Brief")

    real = m._scan_change_homes
    calls = 0

    def counting(control: Path):
        nonlocal calls
        calls += 1
        return real(control)

    monkeypatch.setattr(m, "_scan_change_homes", counting)
    m._overview_lines(tmp_path, show_all=False,
                      now=datetime(2026, 6, 28, tzinfo=UTC), color=False)
    assert calls == 1


def test_overview_lines_renders_inflight_and_backlog(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="A", quest_id="alpha", base_branch="main")
    Change.create_draft(tmp_path, title="Brief", quest_id="bk1",
                        base_branch="main", origin="idea", seed_opening="Brief")

    lines = m._overview_lines(tmp_path, show_all=False,
                              now=datetime(2026, 6, 28, tzinfo=UTC), color=False)
    assert any(ln.startswith("alpha") for ln in lines)   # in-flight zone preserved
    assert "Backlog" in lines                            # backlog section preserved
    assert any(ln.startswith("bk1") for ln in lines)
