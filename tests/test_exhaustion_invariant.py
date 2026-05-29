# tests/test_exhaustion_invariant.py
"""Locks design §5.1: when a step's Retry exceeds max_retries, the LAST event
for that step is step_completed action="retry" — the swapped on_max_retries
action is NEVER logged as a completion event. The renderer therefore cannot and
does not style "exhausted" specially. Regressed twice in git history; this is
the guard."""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.engine.executor import RunResult, run_change


class _NoOutputRunner:
    """Yields no agent_output event — the agent finishes without calling
    complete_step. This is the retryable (non-fatal) path: executor.py:413
    detects agent_output is None and emits step_completed action="retry"."""

    async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
        # Yield nothing; the async generator exits normally.
        return
        yield  # make this an async generator

    async def interrupt(self) -> None:
        pass


@pytest.mark.anyio
async def test_exhausted_step_emits_only_retry_completions(tmp_path: Path) -> None:
    """Characterization lock: exhausted step emits step_completed action="retry"
    for every attempt and never emits a distinct exhaustion-completion event.
    workflow_paused is the only visible signal of exhaustion."""
    # Build a minimal on-disk workflow using the same pattern as test_executor.py.
    # The workflow has max_retries=1 and on_max_retries=Pause (via workflow.yaml
    # defaults), so 2 retryable failures exhaust the step and trigger a pause.
    wf_dir = tmp_path / ".imp" / "workflows" / "default"
    (wf_dir / "agents").mkdir(parents=True)
    (wf_dir / "agents" / "worker.md").write_text(
        "---\nname: worker\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "go {{ change.id }}\n"
    )
    (wf_dir / "workflow.yaml").write_text(
        "name: t\ndescription: t\nphases: [only]\n"
        "defaults: {max_retries: 1, on_max_retries: {kind: pause}}\n"
        "only:\n  steps:\n"
        "    - {name: build, file: agents/worker.md, model_role: default}\n"
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer:\n      - {provider: fake, model: m}\n"
    )

    from imp.providers import resolve

    resolve.reset_providers_for_tests()

    class _NoOutputProvider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return _NoOutputRunner()

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _NoOutputProvider(),
    )

    from imp.engine.change import Change

    change = Change.create(tmp_path, title="t")

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.PAUSED

    # Read events back from the on-disk JsonlEventLog (run_change hard-codes it).
    events = list(change.event_log().read())

    completions = [
        e for e in events
        if e.get("type") == "step_completed"
        and (e.get("payload") or {}).get("step") == "build"
    ]
    assert completions, "step must emit at least one step_completed"

    # Every completion — including the last — is action="retry"; exhaustion is
    # never a distinct completion event.
    assert all(c["payload"]["action"] == "retry" for c in completions), (
        f"expected all step_completed to have action='retry', got: "
        f"{[c['payload']['action'] for c in completions]}"
    )

    types = [e.get("type") for e in events]
    assert "step_exhausted" not in types, (
        f"step_exhausted must not be emitted; full types: {types}"
    )
    assert "phase_exhausted" not in types, (
        f"phase_exhausted must not be emitted; full types: {types}"
    )
    # The only visible signal of exhaustion is its consequence — the pause.
    assert "workflow_paused" in types, (
        f"workflow_paused must be present; full types: {types}"
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"
