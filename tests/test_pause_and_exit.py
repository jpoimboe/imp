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


def test_append_suppressed_when_reason_matches_engine(tmp_path: Path) -> None:
    change = _change(tmp_path, status="running")
    pause_and_exit(change, reason="esc", engine_pause_reason="esc")
    events = [e for e in change.event_log().read() if e["type"] == "workflow_paused"]
    assert events == []


def test_append_when_reason_differs(tmp_path: Path) -> None:
    change = _change(tmp_path, status="running")
    pause_and_exit(change, reason="ctrl-d", engine_pause_reason="esc")
    events = [e for e in change.event_log().read() if e["type"] == "workflow_paused"]
    assert len(events) == 1
    assert events[0]["payload"]["reason"] == "ctrl-d"


def test_append_when_engine_pause_reason_omitted(tmp_path: Path) -> None:
    # Default-None contract (the common path for existing callers): with no
    # engine_pause_reason, the workflow_paused append always fires exactly once.
    change = _change(tmp_path, status="running")
    pause_and_exit(change, reason="ctrl-d")
    events = [e for e in change.event_log().read() if e["type"] == "workflow_paused"]
    assert len(events) == 1
    assert events[0]["payload"]["reason"] == "ctrl-d"
