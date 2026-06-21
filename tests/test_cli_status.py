from pathlib import Path

from imp.cli.main import _status_rows
from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _seed_control


def test_status_lists_inflight_worktrees(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="A", change_id="alpha", base_branch="main")
    Change.create(tmp_path, title="B", change_id="beta", base_branch="main")
    rows = _status_rows(tmp_path)
    slugs = {r[0] for r in rows}
    assert slugs == {"alpha", "beta"}


def test_status_rows_include_errored_and_carry_iso(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create(tmp_path, title="A", change_id="alpha", base_branch="main")
    ch = Change.create(tmp_path, title="B", change_id="beta", base_branch="main")
    # Flip beta to errored: it must still appear in the in-flight default view.
    st = State.load(ch.state_dir)
    st.status = "errored"
    st.save(ch.state_dir)
    # Give alpha a known last event so its row carries a raw ISO timestamp.
    ch_a = Change.load(tmp_path, "alpha")
    ch_a.events_path.write_text('{"timestamp":"2026-06-05T12:00:00+00:00"}\n')

    rows = {r[0]: r for r in _status_rows(tmp_path)}
    assert set(rows) == {"alpha", "beta"}          # errored beta is in-flight
    assert rows["beta"][1] == "errored"
    assert rows["alpha"][3] == "2026-06-05T12:00:00+00:00"   # r[3] is raw ISO, not local


def test_create_seeds_step_label(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="A", change_id="alpha", base_branch="main")
    st = State.load(ch.state_dir)
    flat0_name = st.step                       # the seeded initial cursor
    # The stub workflow's first step has no label:, so step_label falls back to the
    # step NAME — but it must be set (not None), proving the seed wrote it.
    assert flat0_name is not None
    assert st.step_label == flat0_name
