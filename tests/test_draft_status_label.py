from __future__ import annotations

from pathlib import Path

from imp.cli import change_row
from imp.cli import main as m
from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _seed_control


def _park_draft(tmp_path: Path, cid: str) -> Change:
    ch = Change.create_draft(tmp_path, title="X", change_id=cid, base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)
    return ch


def test_row_for_id_labels_parked_draft(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _park_draft(tmp_path, "drflbl")
    row = m._row_for_id(tmp_path, "drflbl")
    assert row is not None
    assert row[1] == "draft"


def test_materialized_paused_change_is_not_labeled_draft(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", change_id="matpause", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)
    row = m._row_for_id(tmp_path, "matpause")
    assert row is not None
    assert row[1] == "paused"   # materialized ⇒ real status, never relabeled


def test_parked_draft_is_backlog_and_resumable(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _park_draft(tmp_path, "drfres")
    assert "drfres" not in {r[0] for r in m._status_rows(tmp_path)}   # not Active
    assert "drfres" in {r[0] for r in m._backlog_rows(tmp_path)}      # in Backlog
    assert "drfres" in {r[0] for r in m._resumable_rows(tmp_path)}    # resumable


def test_draft_status_has_a_color() -> None:
    assert "draft" in change_row.STATUS_COLORS


def test_parked_draft_renders_bare_status_word(tmp_path: Path) -> None:
    from datetime import UTC, datetime
    _seed_control(tmp_path)
    _park_draft(tmp_path, "drbare")
    row = next(r for r in m._backlog_rows(tmp_path) if r[0] == "drbare")
    line = change_row.format_change_line(
        change_id=row[0], title=row[4], status=row[1], step=row[2],
        step_label=row[6], last_iso=row[3], now=datetime.now(UTC), color=False)
    # A ready Backlog draft has an empty step slot ⇒ bare provenance word, no paren.
    # The draft's title is "X" (no parens), so a bare cell leaves no "(" anywhere.
    assert "draft" in line
    assert "(" not in line
