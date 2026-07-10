from __future__ import annotations

from pathlib import Path

from imp.cli import main as m
from imp.engine.change import Change
from tests.conftest import _seed_control


def test_row_for_id_tolerates_missing_state_json(tmp_path: Path) -> None:
    """A change home mid-creation has change.json written but state/state.json
    not yet — the remote daemon's scan loop must not crash on that window
    (matches _scan_quest_homes, which already tolerates it)."""
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="X", quest_id="partial", base_branch="main")
    (ch.state_dir / "state.json").unlink()  # simulate the mid-creation window
    row = m._row_for_id(tmp_path, "partial")
    assert row is None
