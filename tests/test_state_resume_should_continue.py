from __future__ import annotations

import json
from pathlib import Path

from imp.engine.state import State


def test_resume_should_continue_defaults_false(tmp_path: Path) -> None:
    sd = tmp_path / "state"
    State(id="c", step="s", step_attempt=0, status="new").save(sd)
    assert State.load(sd).resume_should_continue is False


def test_arm_then_save_roundtrips_the_pair(tmp_path: Path) -> None:
    sd = tmp_path / "state"
    s = State(id="c", step="s", step_attempt=0, status="new")
    s.arm_step_resume(should_continue=True)
    s.save(sd)
    loaded = State.load(sd)
    assert loaded.resume_pending == "s"
    assert loaded.resume_should_continue is True


def test_save_payload_has_resume_should_continue_not_old_booleans(tmp_path: Path) -> None:
    sd = tmp_path / "state"
    s = State(id="c", step="s", step_attempt=0, status="new")
    s.arm_step_resume(should_continue=False)
    s.save(sd)
    data = json.loads((sd / "state.json").read_text())
    assert data["resume_should_continue"] is False
    assert "resume_mode" not in data
    assert data["resume_pending"] == "s"
    assert "interrupt_pending" not in data
    assert "retry_continue_pending" not in data
