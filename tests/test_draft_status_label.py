from __future__ import annotations

from pathlib import Path

from imp.cli import main as m
from imp.cli import quest_row
from imp.cli import store as s
from imp.engine.quest import Quest
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
    ch = Quest.create_draft(tmp_path, title="X", quest_id="phlbl1", base_branch="main")
    assert State.load(ch.state_dir).phase_label == "intake phase"


def test_create_draft_phase_label_none_when_phase_unlabeled(tmp_path: Path) -> None:
    _seed_control(tmp_path)  # the seeded phase 'p' has no label
    ch = Quest.create_draft(tmp_path, title="X", quest_id="phlbl0", base_branch="main")
    assert State.load(ch.state_dir).phase_label is None


def _park_draft(tmp_path: Path, cid: str) -> Quest:
    ch = Quest.create_draft(tmp_path, title="X", quest_id=cid, base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)
    return ch


def test_row_for_id_labels_parked_draft_as_paused(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _park_draft(tmp_path, "drflbl")
    row = s._row_for_id(tmp_path, "drflbl")
    assert row is not None
    assert row[1] == "paused"


def test_materialized_paused_change_is_not_labeled_draft(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="X", quest_id="matpause", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)
    row = s._row_for_id(tmp_path, "matpause")
    assert row is not None
    assert row[1] == "paused"   # materialized ⇒ real status, never relabeled


def test_parked_draft_is_active_and_resumable(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _park_draft(tmp_path, "drfres")
    assert "drfres" in {r[0] for r in s._status_rows(tmp_path)}       # Active
    assert "drfres" not in {r[0] for r in s._backlog_rows(tmp_path)}  # not Backlog
    assert "drfres" in {r[0] for r in m._resumable_rows(tmp_path)}    # resumable



def test_parked_draft_renders_real_status_word(tmp_path: Path) -> None:
    from datetime import UTC, datetime
    _seed_control(tmp_path)
    _park_draft(tmp_path, "drbare")
    row = next(r for r in s._status_rows(tmp_path) if r[0] == "drbare")
    line = quest_row.format_quest_line(
        quest_id=row[0], title=row[4], status=row[1], step=row[2],
        step_label=row[6], last_iso=row[3], now=datetime.now(UTC), color=False)
    assert "paused" in line


def _parked_default_conversation(root: Path) -> Quest:
    _seed_control(root)
    ch = Quest.create_draft(
        root, title="X", quest_id="dflt01", base_branch="main", workflow="default")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)
    return ch


def test_default_workflow_quest_seeds_the_conversation_labels(tmp_path: Path) -> None:
    state = State.load(_parked_default_conversation(tmp_path).state_dir)
    assert state.step == "threshold"
    assert state.step_label == "chatting"
    assert state.phase_label == "scoping phase"


def test_parked_default_conversation_reads_friendly_on_every_surface(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from imp.cli.resume_select import Candidate, ResumePicker
    from imp.cli.store import make_store

    _parked_default_conversation(tmp_path)
    now = datetime.now(UTC)
    shown = "paused (scoping phase · chatting)"

    overview = m._overview_lines(tmp_path, show_all=False, now=now, color=False)
    assert any("dflt01" in line and shown in line for line in overview)

    r = next(r for r in m._resumable_rows(tmp_path) if r[0] == "dflt01")
    picker = ResumePicker(
        [Candidate(quest_id=r[0], title=r[4], status=r[1], step=r[2], last_iso=r[3],
                   step_label=r[6], phase_label=r[7])],
        now=now, color=False, header="")
    assert shown in picker.render()

    card = make_store(tmp_path).quest_card("dflt01")
    assert card is not None
    lines = m._quest_card(card, now=now, color=False, ledger=None)
    assert any(line.startswith("step:") and line.endswith("scoping phase · chatting")
               for line in lines)


def test_shipped_label_over_the_status_budget_drops_to_the_bare_word() -> None:
    from datetime import UTC, datetime

    from imp.assets import bundled_workflow_dir
    from imp.engine.loader import load_workflow

    step = next(s for s in load_workflow(bundled_workflow_dir("default")).flat_steps()
                if s.name == "scope-code-check")
    line = quest_row.format_quest_line(
        quest_id="dflt01", title="X", status="paused", step=step.name,
        step_label=step.label, phase_label=step.phase_label,
        last_iso=None, now=datetime.now(UTC), color=False)
    assert "paused" in line
    assert "(" not in line
    assert "scoping phase" not in line
    assert "stray" not in line
