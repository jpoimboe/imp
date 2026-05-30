# tests/test_exhaustion_invariant.py
"""Locks the unified-step-model exhaustion behaviour (design §5.1):

When a step exhausts max_retries, the executor emits exactly ONE
step_completed event with action.verb == on_max_retries.verb (typically
"pause"). There is NO separate "step_exhausted" or "phase_exhausted" event,
and no per-retry step_completed events — retries recurse silently.
The only outwardly-visible signal of exhaustion is the resulting
workflow_paused event. Regressed twice in git history; this is the guard.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.engine.executor import RunResult, run_change


class _NoOutputRunner:
    """Yields no agent_output event — the agent finishes without calling
    complete_step. This is the retryable (non-fatal) path: executor.py
    detects agent_output is None and increments step_attempt."""

    async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
        # Yield nothing; the async generator exits normally.
        return
        yield  # make this an async generator

    async def interrupt(self) -> None:
        pass


@pytest.mark.anyio
async def test_exhausted_step_emits_single_pause_completion(tmp_path: Path) -> None:
    """Characterization lock: exhausted step emits exactly one step_completed
    with action.verb == 'pause' (the on_max_retries action) and no spurious
    exhaustion events. workflow_paused is the only visible side-effect."""
    wf_dir = tmp_path / ".imp" / "workflows" / "default"
    (wf_dir / "agents").mkdir(parents=True)
    (wf_dir / "agents" / "worker.md").write_text(
        "---\nname: worker\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "go {{ change.id }}\n"
    )
    # New inline-surface format: phases is a list of {name, steps}.
    # on_max_retries uses verb-style: {pause: true}.
    (wf_dir / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - {name: build, file: agents/worker.md, model_role: default}\n"
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

    # In the unified-step model, retries are silent (no step_completed per retry).
    # Exhaustion emits exactly ONE step_completed with action.verb == on_max_retries verb.
    # With on_max_retries={pause: true}, that is {"verb": "pause"}.
    assert len(completions) == 1, (
        f"expected exactly 1 step_completed (exhaustion), got {len(completions)}: "
        f"{[c['payload']['action'] for c in completions]}"
    )
    exhaustion_action = completions[0]["payload"]["action"]
    assert isinstance(exhaustion_action, dict), (
        f"action must be a dict, got {type(exhaustion_action)}: {exhaustion_action!r}"
    )
    assert exhaustion_action.get("verb") == "pause", (
        f"exhaustion step_completed must have action.verb='pause', "
        f"got: {exhaustion_action!r}"
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
