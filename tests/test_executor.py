# tests/test_executor.py
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from imp.engine.loader import load_workflow
from imp.engine.state import State
from imp.providers.base import Provider


class _FakeRunner:
    def __init__(self, output: dict) -> None:
        self._output = output

    async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
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


def _seed_minimal_workflow(root: Path) -> Change:
    """Create a minimal workflow on disk and snapshot it into a Change."""
    wf_dir = root / ".imp" / "workflows" / "default"
    (wf_dir / "agents").mkdir(parents=True)
    (wf_dir / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "go {{ change.id }}\n"
    )
    (wf_dir / "workflow.yaml").write_text(
        "name: t\ndescription: t\nphases: [only]\n"
        "defaults: {max_retries: 1, on_max_retries: {kind: pause}}\n"
        "only:\n  steps:\n    - {name: w, file: agents/writer.md, model_role: default}\n"
    )
    config = root / ".imp" / "config.yaml"
    config.write_text(
        "roles:\n  default:\n    prefer:\n      - {provider: fake, model: m}\n"
    )
    change = Change.create(root, title="t")
    return change


@pytest.mark.anyio
async def test_run_change_completes_minimal_workflow(tmp_path: Path, monkeypatch) -> None:
    change = _seed_minimal_workflow(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "fake", env_check=lambda: True,
        factory=lambda model: _FakeProvider({"status": "Success"}, model=model),
    )

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED

    state = State.load(change.state_dir)
    assert state.status == "completed"


@pytest.fixture
def anyio_backend():
    return "asyncio"
