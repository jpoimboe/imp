from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from imp.cli.main import _sort_rows_started_first
from imp.cli.store import _status_row
from imp.engine.quest import LAYOUT_VERSION
from imp.engine.state import State
from imp.paths import project_state_dir
from tests.conftest import _seed_control


def _state(**kw: object) -> bytes:
    base = {"status": "running", "step": "write"}
    base.update(kw)
    return json.dumps(base).encode()


_QUEST = json.dumps(
    {"title": "T", "created_at": "2026-05-01T00:00:00+00:00"}).encode()


def test_status_row_carries_started_at_at_index_8() -> None:
    row = _status_row(
        "q1", _state(started_at="2026-06-01T00:00:00+00:00"), _QUEST,
        "2026-06-02T00:00:00+00:00")
    assert row is not None
    assert row[8] == "2026-06-01T00:00:00+00:00"


def test_status_row_started_at_is_none_when_absent() -> None:
    row = _status_row("q1", _state(), _QUEST, "2026-06-02T00:00:00+00:00")
    assert row is not None
    assert row[8] is None


_NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)


def _row(cid: str, created: str, started: str | None) -> tuple:
    # (id, status, step, last, title, created_at, step_label, phase_label, started_at)
    return (cid, "running", "write", "L", "T", created, None, None, started)


def test_sort_started_first_ascending_with_created_at_fallback() -> None:
    rows = [
        _row("c", "2026-01-03T00:00:00+00:00", "2026-06-03T00:00:00+00:00"),
        _row("a", "2026-01-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00"),
        _row("b", "2026-01-02T00:00:00+00:00", None),  # no start -> created_at key
    ]
    # keys ascending: b(01-02) < a(06-01) < c(06-03)
    assert [r[0] for r in _sort_rows_started_first(rows)] == ["b", "a", "c"]


def test_sort_started_first_tiebreak_created_at_then_id() -> None:
    same = "2026-06-01T00:00:00+00:00"
    rows = [
        _row("z", "2026-01-02T00:00:00+00:00", same),
        _row("y", "2026-01-01T00:00:00+00:00", same),
        _row("x", "2026-01-02T00:00:00+00:00", same),
    ]
    # equal started_at -> created_at asc (y first), then id asc (x before z)
    assert [r[0] for r in _sort_rows_started_first(rows)] == ["y", "x", "z"]


def _seed_inflight(control: Path, cid: str, *, created: str, started: str) -> None:
    cdir = project_state_dir(control) / "quests" / cid
    (cdir / "state").mkdir(parents=True)
    (cdir / "quest.json").write_text(json.dumps({
        "id": cid, "title": cid.upper(), "base_branch": "main",
        "created_at": created, "materialized": True,
        "layout_version": LAYOUT_VERSION}))
    (cdir / "events.jsonl").write_text(json.dumps({
        "type": "step_started", "agent_id": None, "timestamp": created,
        "payload": {"step": "write", "phase": "p", "phase_label": "impl"}}) + "\n")
    State(id=cid, status="running", step="write", step_attempt=0,
          step_label="coding", phase_label="impl", started_at=started).save(cdir / "state")


def test_overview_orders_inflight_by_started_at_ascending(tmp_path: Path) -> None:
    import imp.cli.main as m
    _seed_control(tmp_path)
    _seed_inflight(tmp_path, "late01", created="2026-06-01T00:00:00+00:00",
                   started="2026-06-03T00:00:00+00:00")
    _seed_inflight(tmp_path, "erly01", created="2026-06-02T00:00:00+00:00",
                   started="2026-06-01T00:00:00+00:00")
    lines = m._overview_lines(tmp_path, show_all=False, now=_NOW, color=False)
    ids = [ln[:6] for ln in lines if ln.startswith(("late01", "erly01"))]
    assert ids == ["erly01", "late01"]        # oldest start at the top


def test_filtered_active_orders_inflight_by_started_at_ascending(tmp_path: Path) -> None:
    import imp.cli.main as m
    _seed_control(tmp_path)
    _seed_inflight(tmp_path, "late02", created="2026-06-01T00:00:00+00:00",
                   started="2026-06-03T00:00:00+00:00")
    _seed_inflight(tmp_path, "erly02", created="2026-06-02T00:00:00+00:00",
                   started="2026-06-01T00:00:00+00:00")
    lines = m._filtered_overview_lines(
        tmp_path, dispositions={"active"}, cutoff=None, now=_NOW, color=False)
    ids = [ln[:6] for ln in lines if ln.startswith(("late02", "erly02"))]
    assert ids == ["erly02", "late02"]
