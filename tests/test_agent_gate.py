"""AgentGate execution end-to-end via the engine, using a fake provider.

Mirrors test_executor.py's _FakeProvider pattern. The fake returns a canned
output dict from any invocation; the decide block matches on that and yields
an action.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from imp.engine.state import State
from imp.providers import resolve


class _FakeRunner:
    def __init__(self, output: dict) -> None:
        self._output = output

    async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
        yield {"type": "agent_message", "payload": {"role": "assistant", "content": []}}
        yield {"type": "agent_output", "payload": self._output}

    async def interrupt(self) -> None:
        pass


class _FakeProvider:
    name = "fake"

    def __init__(self, output: dict, model: str = "m") -> None:
        self._output = output
        self.model = model

    def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
        return _FakeRunner(self._output)


def _seed_workflow_with_agent_gate(
    tmp_path: Path, *, gate_decide_yaml: str,
) -> Change:
    """Workflow: phase A with a writer step + a phase-level agent gate.
    Phase B is empty. The agent gate's decide is parameterized."""
    wf = tmp_path / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "go\n"
    )
    (wf / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "  verdict: {type: enum, values: [approve, rework]}\n---\n"
        "review\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\nphases: [a, b]\n"
        "defaults: {max_retries: 1, on_max_retries: {kind: pause}}\n"
        "a:\n"
        "  steps:\n"
        "    - {name: w, file: agents/writer.md, model_role: default}\n"
        "  gates:\n"
        "    - type: agent\n"
        "      file: agents/reviewer.md\n"
        "      model_role: default\n"
        f"{gate_decide_yaml}\n"
        "b:\n  steps: []\n"
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer:\n      - {provider: fake, model: m}\n"
    )
    return Change.create(tmp_path, title="t")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_agent_gate_continue_on_approve(tmp_path: Path) -> None:
    change = _seed_workflow_with_agent_gate(tmp_path, gate_decide_yaml=(
        "      decide:\n"
        "        - {when: {verdict: approve}, action: {kind: continue}}\n"
        "        - {when: {verdict: rework}, action: {kind: retry}}\n"
        "        - {else: true, action: {kind: pause}}\n"
    ))

    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True,
        factory=lambda model: _FakeProvider(
            {"status": "Success", "verdict": "approve"}, model=model,
        ),
    )

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED

    state = State.load(change.state_dir)
    assert state.status == "completed"


@pytest.mark.anyio
async def test_agent_gate_retry_then_continue(tmp_path: Path) -> None:
    """First invocation: verdict=rework → retry. Second invocation: approve → continue."""
    change = _seed_workflow_with_agent_gate(tmp_path, gate_decide_yaml=(
        "      decide:\n"
        "        - {when: {verdict: approve}, action: {kind: continue}}\n"
        "        - {when: {verdict: rework}, action: {kind: retry}}\n"
        "        - {else: true, action: {kind: pause}}\n"
    ))

    # Sequence: writer always succeeds; reviewer rejects once then approves.
    outputs = iter([
        {"status": "Success"},                              # writer
        {"status": "Success", "verdict": "rework"},          # reviewer 1st
        {"status": "Success"},                              # writer retry
        {"status": "Success", "verdict": "approve"},         # reviewer 2nd
    ])
    class _SeqProvider:
        name = "fake"
        def __init__(self, model: str) -> None: self.model = model
        def create_runner(self, _inv):
            return _FakeRunner(next(outputs))

    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True, factory=_SeqProvider,
    )

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED


@pytest.mark.anyio
async def test_agent_gate_pause(tmp_path: Path) -> None:
    """Reviewer returns verdict=approve, but the workflow's decide block
    has no `when: {verdict: approve}` rule — falls through to `else: pause`."""
    # The reviewer's output_schema requires verdict ∈ {approve, rework}, so
    # we can't return an "uncertain" value to force the else branch. Instead
    # parameterize the workflow's decide to lack any matching `when:` for
    # the returned verdict.
    change = _seed_workflow_with_agent_gate(tmp_path, gate_decide_yaml=(
        "      decide:\n"
        "        - {when: {verdict: rework}, action: {kind: retry}}\n"
        "        - {else: true, action: {kind: pause}}\n"
    ))

    outputs = iter([
        {"status": "Success"},
        {"status": "Success", "verdict": "approve"},   # falls through to else
    ])
    class _SeqProvider:
        name = "fake"
        def __init__(self, model: str) -> None: self.model = model
        def create_runner(self, _inv):
            return _FakeRunner(next(outputs))

    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True, factory=_SeqProvider,
    )

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.PAUSED


@pytest.mark.anyio
async def test_agent_gate_goto(tmp_path: Path) -> None:
    """verdict=rework with goto action jumps to target phase."""
    # Build a 3-phase workflow with goto: target a from b's gate.
    wf = tmp_path / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "go\n"
    )
    (wf / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "  verdict: {type: enum, values: [approve, rework]}\n---\n"
        "review\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\nphases: [a, b]\n"
        "defaults: {max_retries: 1, on_max_retries: {kind: pause}}\n"
        "a:\n"
        "  steps:\n"
        "    - {name: w, file: agents/writer.md, model_role: default}\n"
        "b:\n"
        "  steps: []\n"
        "  gates:\n"
        "    - type: agent\n"
        "      file: agents/reviewer.md\n"
        "      model_role: default\n"
        "      decide:\n"
        "        - {when: {verdict: approve}, action: {kind: continue}}\n"
        "        - {when: {verdict: rework}, action: {kind: goto, target: a}}\n"
        "        - {else: true, action: {kind: pause}}\n"
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer:\n      - {provider: fake, model: m}\n"
    )
    change = Change.create(tmp_path, title="t")

    # Sequence: writer / reviewer-rework / writer-retry / reviewer-approve
    outputs = iter([
        {"status": "Success"},                                # writer a
        {"status": "Success", "verdict": "rework"},            # reviewer b → goto a
        {"status": "Success"},                                # writer a (again)
        {"status": "Success", "verdict": "approve"},           # reviewer b approves
    ])
    class _SeqProvider:
        name = "fake"
        def __init__(self, model: str) -> None: self.model = model
        def create_runner(self, _inv):
            return _FakeRunner(next(outputs))

    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True, factory=_SeqProvider,
    )

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED


@pytest.mark.anyio
async def test_agent_gate_no_match_retries(tmp_path: Path) -> None:
    """decide block without else: true and no match → retry (defensive)."""
    change = _seed_workflow_with_agent_gate(tmp_path, gate_decide_yaml=(
        "      decide:\n"
        "        - {when: {verdict: approve}, action: {kind: continue}}\n"
        "        - {when: {verdict: rework}, action: {kind: retry}}\n"
        # No else clause. Loader emits a WARNING (validator #13). We accept it.
    ))

    # Reviewer always returns rework → first eval matches → retry → max_retries hits → pause.
    outputs = iter([
        {"status": "Success"}, {"status": "Success", "verdict": "rework"},
        {"status": "Success"}, {"status": "Success", "verdict": "rework"},
    ])
    class _SeqProvider:
        name = "fake"
        def __init__(self, model: str) -> None: self.model = model
        def create_runner(self, _inv):
            return _FakeRunner(next(outputs))

    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True, factory=_SeqProvider,
    )

    # max_retries=1 in defaults → 1 retry attempt → on_max_retries=pause.
    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.PAUSED
