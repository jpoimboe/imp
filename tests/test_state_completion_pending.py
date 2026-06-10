from __future__ import annotations

from pathlib import Path

from imp.engine.state import State


def test_completion_pending_defaults_false_and_roundtrips(tmp_path: Path) -> None:
    sd = tmp_path / "state"
    State(id="c", step="s", step_attempt=0, status="running").save(sd)
    assert State.load(sd).completion_pending is False

    s = State.load(sd)
    s.completion_pending = True
    s.save(sd)
    assert State.load(sd).completion_pending is True


def test_completion_pending_absent_in_old_json_loads_false(tmp_path: Path) -> None:
    sd = tmp_path / "state"
    State(id="c", step="s", step_attempt=0, status="running").save(sd)
    # Simulate an older state.json with no completion_pending key.
    import json
    p = sd / "state.json"
    data = json.loads(p.read_text())
    data.pop("completion_pending", None)
    p.write_text(json.dumps(data))
    assert State.load(sd).completion_pending is False
