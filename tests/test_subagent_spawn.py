# tests/test_subagent_spawn.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers import resolve

_SRC = SourceLoc(file=Path("a.md"), line=1)


def _agent(name: str) -> AgentDef:
    return AgentDef(
        name=name, description="", tools=frozenset(), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error")),), source=_SRC),
        prompt_template_source="", source_path=Path("a.md"), source=_SRC)


class _RecordingRunner:
    def __init__(self, name, model):
        self.name = name
        self.model = model

    async def run(self, invocation):
        yield {"type": "agent_output",
               "payload": {"status": "Success", "ran_on": self.name,
                           "model": self.model}}


class _RecordingProvider:
    def __init__(self, name, model):
        self.name = name
        self.model = model

    def create_runner(self, invocation):
        return _RecordingRunner(self.name, self.model)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _providers():
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "anthropic", env_check=lambda: True,
        factory=lambda m: _RecordingProvider("anthropic", m))
    resolve.register_provider(
        "openai", env_check=lambda: True,
        factory=lambda m: _RecordingProvider("openai", m))
    yield
    resolve.reset_providers_for_tests()


def _inv(provider, model, tmp_path):
    return AgentInvocation(
        agent_def=_agent("rev"), provider_name=provider, model_name=model,
        system_prompt="", initial_messages=(),
        change_context=ChangeContext(
            tmp_path, tmp_path, tmp_path / ".imp" / "change" / "workflow"),
        transcript_dir=tmp_path, fs_permissions=(),
        events_path=tmp_path / "e.jsonl")


@pytest.mark.anyio
async def test_spawn_builds_runner_of_resolved_provider(tmp_path):
    """A subagent whose invocation names openai runs on openai's runner, even
    though the parent might be anthropic."""
    runner = await resolve.spawn_subagent_via_registry(_inv("openai", "gpt-5.2", tmp_path))
    outs = [e async for e in runner.run(_inv("openai", "gpt-5.2", tmp_path))]
    assert outs[-1]["payload"]["ran_on"] == "openai"
    assert outs[-1]["payload"]["model"] == "gpt-5.2"


@pytest.mark.anyio
async def test_spawn_subagent_via_registry_returns_runner(tmp_path) -> None:
    from imp.providers.resolve import spawn_subagent_via_registry

    resolve.reset_providers_for_tests()

    class _Runner:
        async def run(self, inv):
            yield {"type": "agent_output", "payload": {"status": "Success"}}
        async def interrupt(self) -> None: ...

    class _Provider:
        name = "fake"
        model = "m"
        def create_runner(self, _inv):
            return _Runner()

    resolve.register_provider("fake", env_check=lambda: True,
                              factory=lambda _m: _Provider())

    agent = AgentDef(
        name="a", description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="hi", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1))
    inv = AgentInvocation(
        agent_def=agent, provider_name="fake", model_name="m",
        system_prompt="s", initial_messages=(),
        change_context=ChangeContext(title="t",
                                     project_root=tmp_path,
                                     workflow_dir=tmp_path / "wf"),
        transcript_dir=tmp_path / "td",
        fs_permissions=(), events_path=tmp_path / "e.jsonl")

    runner = await spawn_subagent_via_registry(inv)
    # A runner, not a generator: it exposes run() + interrupt().
    assert hasattr(runner, "run") and hasattr(runner, "interrupt")
