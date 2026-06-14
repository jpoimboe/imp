from __future__ import annotations

import json
from pathlib import Path

from imp.agents.runner import Message, TextBlock
from imp.agents.transcript import Transcript
from imp.cli.main import _pause_lines, _recover_phase
from imp.engine.change import Change
from imp.engine.state import State


def test_pause_lines_include_from_when_recover_phase_set() -> None:
    lines = _pause_lines("title", "spec", "k3f9qa",
                         reason="esc", color=False, recover_phase="spec")
    joined = "\n".join(lines)
    assert "imp resume k3f9qa --from spec" in joined


def test_pause_lines_omit_from_without_recover_phase() -> None:
    lines = _pause_lines("title", "spec", "k3f9qa",
                         reason="esc", color=False)
    assert all("--from" not in ln for ln in lines)


def _make_change(root: Path) -> Change:
    change_dir = root / ".imp" / "change"
    (change_dir / "state").mkdir(parents=True)
    (change_dir / "change.json").write_text(json.dumps(
        {"id": "c", "title": "t", "base_branch": "main"}, sort_keys=True))
    State(id="c", step="review", step_attempt=0, status="paused",
          vars={}, loop_counts={}, resume_pending="review").save(change_dir / "state")
    change = Change(id="c", title="t", base_branch="main",
                    control_root=root, project_root=root, change_dir=change_dir)
    change.event_log().append(
        agent_id=None, type="step_started",
        payload={"step": "review", "phase": "spec"})
    return change


def test_recover_phase_detects_truncated_autonomous(tmp_path: Path) -> None:
    change = _make_change(tmp_path)
    tdir = change.change_dir / "steps" / "spec" / "review"
    tdir.mkdir(parents=True)
    Transcript(tdir / "transcript.jsonl").append(
        Message(role="assistant", content=(TextBlock(text="reviewing"),)))
    assert _recover_phase(change) == "spec"


def test_recover_phase_none_when_no_kept_transcript(tmp_path: Path) -> None:
    change = _make_change(tmp_path)  # no transcript on disk
    assert _recover_phase(change) is None


def test_recover_phase_none_when_transcript_is_complete(tmp_path: Path) -> None:
    change = _make_change(tmp_path)
    tdir = change.change_dir / "steps" / "spec" / "review"
    tdir.mkdir(parents=True)
    # A complete transcript ends with a user turn (SEND_TO_LLM state), so
    # _resume_replays_transcript returns True → _recover_phase returns None.
    Transcript(tdir / "transcript.jsonl").append(
        Message(role="user", content=(TextBlock(text="done"),)))
    assert _recover_phase(change) is None
