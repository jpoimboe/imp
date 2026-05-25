"""Resume-skip-step: cached agent_output short-circuits the runner on resume,
but only on the first attempt (step_attempt == 0); retries re-invoke.
Cache keys on (phase, step_name) so same-named steps across phases don't collide.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation
from imp.engine.change import Change
from imp.engine.executor import run
from imp.engine.state import State

FIXTURES = Path(__file__).parent / "fixtures"


class _Counter:
    def __init__(self, output: dict[str, Any]) -> None:
        self.calls = 0
        self.output = output

    def run(self, inv: AgentInvocation) -> Iterator[dict[str, Any]]:
        self.calls += 1
        yield {"type": "agent_output", "payload": self.output}


def test_resume_skips_step_with_cached_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    shutil.copytree(
        FIXTURES / "workflows" / "integration",
        tmp_path / ".imp" / "workflows" / "default",
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: [{provider: anthropic, model: x}]}\n"
    )
    monkeypatch.chdir(tmp_path)
    change = Change.create(tmp_path, title="t")

    runner = _Counter({"status": "Success"})
    run(change, runner)  # type: ignore[arg-type]
    # Integration fixture has 2 step invocations before the script gate pauses
    assert runner.calls == 2

    # Flip script gate from `false` to `true` so resume passes
    snap = change.workflow_dir / "workflow.yaml"
    snap.write_text(snap.read_text().replace('"false"', '"true"'))

    state = State.load(change.state_dir)
    state.status = "running"
    state.save(change.state_dir)

    runner2 = _Counter({"status": "Success"})
    run(change, runner2)  # type: ignore[arg-type]
    # On resume: write.coder is cached → skipped. verify.context-loader runs.
    # Even though there's an earlier load.context-loader output, the
    # (phase, step) keying prevents it from satisfying verify.context-loader.
    assert runner2.calls == 1


def test_resume_skip_disabled_on_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """If step_attempt > 0 (we're retrying), the cache is ignored — agent
    re-runs. Otherwise a Retry from a gate would loop forever against the
    same cached output."""
    shutil.copytree(
        FIXTURES / "workflows" / "integration",
        tmp_path / ".imp" / "workflows" / "default",
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: [{provider: anthropic, model: x}]}\n"
    )
    monkeypatch.chdir(tmp_path)
    change = Change.create(tmp_path, title="t")

    # Manually seed an event_log with a step_started + agent_output for
    # write.coder, and set state to a mid-attempt retry.
    log = change.event_log()
    log.append("phase_started", {"phase": "load"})
    log.append("step_started", {"step": "context-loader", "phase": "load"})
    log.append("agent_output", {"status": "Success"})
    log.append("phase_started", {"phase": "write"})
    log.append("step_started", {"step": "coder", "phase": "write"})
    log.append("agent_output", {"status": "Success"})
    state = State(
        change_id=1, phase="write", step="coder", step_attempt=1,
        phase_attempt=0, status="running",
    )
    state.save(change.state_dir)

    runner = _Counter({"status": "Success"})
    # Patch the script gate to `true` so it doesn't trip
    snap = change.workflow_dir / "workflow.yaml"
    snap.write_text(snap.read_text().replace('"false"', '"true"'))
    run(change, runner)  # type: ignore[arg-type]
    # step_attempt > 0 → cache ignored → coder runs again.
    # Then verify.context-loader runs.
    assert runner.calls >= 2
