import json
from pathlib import Path

from imp.engine.state import State


def test_state_round_trip(tmp_path: Path) -> None:
    s = State(
        change_id=5,
        phase="onboard",
        step="intent",
        step_attempt=1,
        phase_attempt=0,
        status="running",
    )
    s.save(tmp_path)
    loaded = State.load(tmp_path)
    assert loaded == s


def test_state_atomic_overwrite(tmp_path: Path) -> None:
    s = State(
        change_id=1, phase="p1", step=None, step_attempt=0, phase_attempt=0,
        status="running",
    )
    s.save(tmp_path)
    s2 = State(
        change_id=1, phase="p2", step="s", step_attempt=2, phase_attempt=1,
        status="running",
    )
    s2.save(tmp_path)
    raw = json.loads((tmp_path / "state.json").read_text())
    assert raw["phase"] == "p2"
    assert raw["step_attempt"] == 2


def test_state_no_partial_writes(tmp_path: Path) -> None:
    s = State(
        change_id=1, phase="p", step=None, step_attempt=0, phase_attempt=0,
        status="running",
    )
    s.save(tmp_path)
    # No leftover tmp file
    siblings = list(tmp_path.iterdir())
    assert {p.name for p in siblings} == {"state.json"}
