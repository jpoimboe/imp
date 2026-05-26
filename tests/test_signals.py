"""Pause-on-interrupt: KeyboardInterrupt + EOFError save state and exit clean."""

from __future__ import annotations

from pathlib import Path

from imp.cli.signals import pause_and_exit
from imp.engine.change import Change
from imp.engine.state import State


def _seed_change(tmp_path: Path) -> Change:
    (tmp_path / ".imp" / "workflows" / "default").mkdir(parents=True)
    (tmp_path / ".imp" / "workflows" / "default" / "workflow.yaml").write_text(
        "name: x\ndescription: y\nphases: [p]\n"
        "defaults:\n  max_retries: 0\n  on_max_retries: {kind: pause}\n"
        "p:\n  steps: []\n"
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: [{provider: anthropic, model: x}]}\n"
    )
    return Change.create(tmp_path, title="t")


def test_pause_writes_state_and_event(tmp_path: Path) -> None:
    change = _seed_change(tmp_path)
    State(
        change_id=change.id, phase="p", step="s",
        step_attempt=1, phase_attempt=0, status="running",
    ).save(change.state_dir)

    pause_and_exit(change, reason="ctrl-c")

    state = State.load(change.state_dir)
    assert state.status == "paused"
    events = list(change.event_log().read())
    assert any(e["type"] == "workflow_paused" for e in events)
    assert any(
        e["type"] == "workflow_paused" and e["payload"].get("reason") == "ctrl-c"
        for e in events
    )


def test_pause_is_idempotent(tmp_path: Path) -> None:
    """Calling twice shouldn't crash or double-append (last write wins)."""
    change = _seed_change(tmp_path)
    State(
        change_id=change.id, phase="p", step=None,
        step_attempt=0, phase_attempt=0, status="running",
    ).save(change.state_dir)
    pause_and_exit(change, reason="x")
    pause_and_exit(change, reason="y")
    state = State.load(change.state_dir)
    assert state.status == "paused"
