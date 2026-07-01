from __future__ import annotations

import json
from pathlib import Path

from imp.engine.state import State


def test_retry_continue_pending_defaults_false_and_roundtrips(tmp_path: Path) -> None:
    sd = tmp_path / "state"
    State(id="c", step="s", step_attempt=0, status="running").save(sd)
    assert State.load(sd).retry_continue_pending is False

    s = State.load(sd)
    s.retry_continue_pending = True
    s.save(sd)
    assert State.load(sd).retry_continue_pending is True


def test_retry_continue_pending_absent_in_old_json_loads_false(tmp_path: Path) -> None:
    sd = tmp_path / "state"
    State(id="c", step="s", step_attempt=0, status="running").save(sd)
    # Simulate an older state.json with no retry_continue_pending key.
    p = sd / "state.json"
    data = json.loads(p.read_text())
    data.pop("retry_continue_pending", None)
    p.write_text(json.dumps(data))
    assert State.load(sd).retry_continue_pending is False
