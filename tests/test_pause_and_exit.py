from __future__ import annotations

import json
from pathlib import Path

from imp.cli.signals import pause_and_exit
from imp.engine.change import Change
from imp.engine.state import State


def _change(tmp_path: Path, status: str) -> Change:
    cd = tmp_path / ".imp" / "change"
    (cd / "state").mkdir(parents=True, exist_ok=True)
    (cd / "change.json").write_text(json.dumps(
        {"id": "c", "title": "t", "base_branch": "main"}))
    State(id="c", step="s", step_attempt=0, status=status).save(cd / "state")
    return Change(id="c", title="t", base_branch="main",
                  control_root=tmp_path, project_root=tmp_path, change_dir=cd)


def test_status_transitions_only_from_running(tmp_path: Path) -> None:
    change = _change(tmp_path, status="merge_blocked")
    pause_and_exit(change, reason="ctrl-d")
    assert State.load(change.state_dir).status == "merge_blocked"


def test_status_paused_when_running(tmp_path: Path) -> None:
    change = _change(tmp_path, status="running")
    pause_and_exit(change, reason="ctrl-d")
    assert State.load(change.state_dir).status == "paused"


def test_no_append_when_open_pause_in_journal(tmp_path: Path) -> None:
    # The executor already emitted a workflow_paused for this pause and nothing
    # resumed since → an open pause exists → pause_and_exit must NOT duplicate it.
    change = _change(tmp_path, status="running")
    change.event_log().append(
        agent_id=None, type="workflow_paused",
        payload={"reason": "ctrl-c", "interrupt_reason": "ctrl-c"})
    pause_and_exit(change, reason="ctrl-c")
    events = [e for e in change.event_log().read() if e["type"] == "workflow_paused"]
    assert len(events) == 1


def test_appends_when_last_pause_was_resumed(tmp_path: Path) -> None:
    # A prior pause was resumed → no open pause → the new pause must be appended.
    change = _change(tmp_path, status="running")
    log = change.event_log()
    log.append(agent_id=None, type="workflow_paused",
               payload={"reason": "ctrl-c", "interrupt_reason": "ctrl-c"})
    log.append(agent_id=None, type="workflow_resumed", payload={})
    pause_and_exit(change, reason="ctrl-c")
    events = [e for e in change.event_log().read() if e["type"] == "workflow_paused"]
    assert len(events) == 2


def test_appends_on_empty_journal(tmp_path: Path) -> None:
    # SIGTERM / clean-solicit path: the engine emitted nothing, so the journal has
    # no open pause and the append is the ONLY workflow_paused.
    change = _change(tmp_path, status="running")
    pause_and_exit(change, reason="sigterm")
    events = [e for e in change.event_log().read() if e["type"] == "workflow_paused"]
    assert len(events) == 1
    assert events[0]["payload"]["reason"] == "sigterm"



def test_signal_path_event_carries_interrupt_reason(tmp_path: Path) -> None:
    # The renderer keys on interrupt_reason (falling back to reason); the
    # signal-path event must carry interrupt_reason so it renders consistently
    # with the executor-path event.
    change = _change(tmp_path, status="running")
    pause_and_exit(change, reason="sigterm")
    events = [e for e in change.event_log().read() if e["type"] == "workflow_paused"]
    assert len(events) == 1
    assert events[0]["payload"]["interrupt_reason"] == "sigterm"
    assert events[0]["payload"]["reason"] == "sigterm"
