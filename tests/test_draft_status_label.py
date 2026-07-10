from __future__ import annotations

from pathlib import Path

from imp.cli import change_row
from imp.cli import main as m
from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _seed_control


def _rewrite_phase_label(root: Path, label: str) -> None:
    wf = root / "imp" / "workflows" / "imp" / "workflow.yaml"
    wf.write_text(
        "name: t\ndescription: \"\"\n"
        "phases:\n"
        f"  - name: p\n    label: {label}\n    steps:\n"
        "      - {name: s, file: agents/stub.md}\n"
    )


def test_create_draft_seeds_phase_label_from_first_step(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _rewrite_phase_label(tmp_path, "intake phase")
    ch = Change.create_draft(tmp_path, title="X", quest_id="phlbl1", base_branch="main")
    assert State.load(ch.state_dir).phase_label == "intake phase"


def test_create_draft_phase_label_none_when_phase_unlabeled(tmp_path: Path) -> None:
    _seed_control(tmp_path)  # the seeded phase 'p' has no label
    ch = Change.create_draft(tmp_path, title="X", quest_id="phlbl0", base_branch="main")
    assert State.load(ch.state_dir).phase_label is None


def _park_draft(tmp_path: Path, cid: str) -> Change:
    ch = Change.create_draft(tmp_path, title="X", quest_id=cid, base_branch="main")
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
    ch = Change.create(tmp_path, title="X", quest_id="matpause", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)
    row = m._row_for_id(tmp_path, "matpause")
    assert row is not None
    assert row[1] == "paused"   # materialized ⇒ real status, never relabeled


def test_parked_draft_is_active_and_resumable(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _park_draft(tmp_path, "drfres")
    assert "drfres" in {r[0] for r in m._status_rows(tmp_path)}       # Active
    assert "drfres" not in {r[0] for r in m._backlog_rows(tmp_path)}  # not Backlog
    assert "drfres" in {r[0] for r in m._resumable_rows(tmp_path)}    # resumable


def test_draft_status_has_a_color() -> None:
    assert "draft" in change_row.STATUS_COLORS


def test_parked_draft_renders_draft_status_word(tmp_path: Path) -> None:
    from datetime import UTC, datetime
    _seed_control(tmp_path)
    _park_draft(tmp_path, "drbare")
    row = next(r for r in m._status_rows(tmp_path) if r[0] == "drbare")
    line = change_row.format_change_line(
        quest_id=row[0], title=row[4], status=row[1], step=row[2],
        step_label=row[6], last_iso=row[3], now=datetime.now(UTC), color=False)
    assert "draft" in line
