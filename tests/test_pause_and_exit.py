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


def test_append_suppressed_when_engine_emitted(tmp_path: Path) -> None:
    # The executor already emitted a workflow_paused for this pause this run →
    # pause_and_exit must NOT duplicate it.
    change = _change(tmp_path, status="running")
    pause_and_exit(change, reason="esc", engine_emitted=True)
    events = [e for e in change.event_log().read() if e["type"] == "workflow_paused"]
    assert events == []


def test_append_when_engine_did_not_emit(tmp_path: Path) -> None:
    # The clean-solicit-SIGINT / SIGTERM paths: the engine emitted nothing, so the
    # append is the ONLY workflow_paused and must fire even when reason coincides
    # with the gesture (the bug: it used to be suppressed by reason-equality).
    change = _change(tmp_path, status="running")
    pause_and_exit(change, reason="sigterm", engine_emitted=False)
    events = [e for e in change.event_log().read() if e["type"] == "workflow_paused"]
    assert len(events) == 1
    assert events[0]["payload"]["reason"] == "sigterm"


def test_append_when_engine_emitted_omitted(tmp_path: Path) -> None:
    # Default-False contract (the common path): with no engine_emitted, the append
    # always fires exactly once.
    change = _change(tmp_path, status="running")
    pause_and_exit(change, reason="ctrl-d")
    events = [e for e in change.event_log().read() if e["type"] == "workflow_paused"]
    assert len(events) == 1
    assert events[0]["payload"]["reason"] == "ctrl-d"
