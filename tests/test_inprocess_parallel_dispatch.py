from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.agents.parallel import reset_parallelism_cache
from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
from imp.agents.tools import get_registry  # noqa: F401 ensures tools registered
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
from imp.providers.anthropic import AnthropicAgentRunner, AnthropicProvider
from imp.providers.config import ProviderModel
from imp.providers.openai import OpenAIAgentRunner, OpenAIProvider
from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use
from tests.openai_fakes import (
    FakeResponsesClient,
    resp_function_call,
    resp_response,
)

_SRC = SourceLoc(file=Path("a.md"), line=1)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _status_schema() -> OutputSchema:
    return OutputSchema(
        fields=(OutputField(name="status", type="enum",
                            values=("Success", "Error"), required=True),),
        source=_SRC)


class _EchoRunner:
    """Child runner: consumes nothing from the parent queue; echoes the task
    description so the parent's task tool_result is identifiable."""

    async def run(self, invocation):
        td = invocation.initial_messages[0].content[0].text
        yield {"type": "agent_output",
               "payload": {"status": "Success", "echo": td}}

    async def interrupt(self) -> None: ...


class _EchoProvider:
    def __init__(self, name: str, model: str) -> None:
        self.name = name
        self.model = model

    def create_runner(self, invocation):
        return _EchoRunner()


def _parent_def() -> AgentDef:
    reviewer = AgentDef(
        name="reviewer", description="", tools=frozenset({"complete_step"}),
        subagents=(), output_schema=_status_schema(),
        prompt_template_source="r", source_path=Path("r.md"), source=_SRC,
        tier="medium")
    return reviewer


def _coder_def() -> AgentDef:
    return AgentDef(
        name="coder", description="",
        tools=frozenset({"complete_step", "task"}), subagents=("reviewer",),
        output_schema=_status_schema(), prompt_template_source="c",
        source_path=Path("c.md"), source=_SRC)


def _inv(tmp_path: Path, log, provider: str) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_coder_def(),
        provider_name=provider, model_name="m",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow"),
        transcript_dir=tmp_path / "step",
        fs_permissions=(FsRule(operations=("read",), paths=("/project/**",),
                               mode="allow", source=_SRC),),
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=log,
        subagent_defs={"reviewer": _parent_def()},
        resolved_tiers={"medium": ProviderModel("rec", "rec-model")},
    )


def _make(provider: str, tmp_path: Path, responses):
    """Build (runner, register-parent-provider) for the named parent provider."""
    if provider == "anthropic":
        fake = FakeAsyncAnthropic(responses["anthropic"])
        runner = AnthropicAgentRunner(model_name="m", client_factory=lambda: fake)
        return runner, ("anthropic",
                        lambda m: AnthropicProvider(model_name=m,
                                                    client_factory=lambda: fake))
    fake = FakeResponsesClient(responses["openai"])
    runner = OpenAIAgentRunner(model_name="m", client_factory=lambda: fake)
    return runner, ("openai",
                    lambda m: OpenAIProvider(model_name=m,
                                             client_factory=lambda: fake))


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
@pytest.mark.anyio
async def test_single_task_turn_routes_through_batch(
    tmp_path: Path, provider: str, provider_registry,
) -> None:
    """AC1 (structural N=1 parity, both loops): a single-task turn produces one
    tool_call THEN one tool_result for that tu and completes with agent_output —
    today's shape, now via the batch helper."""
    (tmp_path / "art").mkdir()
    reset_parallelism_cache()
    responses = {
        "anthropic": [
            response(tool_use("t1", "task",
                              {"subagent_name": "reviewer",
                               "task_description": "only"})),
            response(tool_use("pc", "complete_step", {"status": "Success"})),
        ],
        "openai": [
            resp_response(resp_function_call(
                "t1", "task",
                '{"subagent_name": "reviewer", "task_description": "only"}')),
            resp_response(resp_function_call(
                "pc", "complete_step", '{"status": "Success"}')),
        ],
    }
    runner, (pname, pfactory) = _make(provider, tmp_path, responses)
    provider_registry(pname, env_check=lambda: True, factory=pfactory)
    provider_registry("rec", env_check=lambda: True,
                      factory=lambda m: _EchoProvider("rec", m))
    log = InMemoryEventLog()
    events = [e async for e in runner.run(_inv(tmp_path, log, provider))]
    reset_parallelism_cache()

    assert events[-1]["type"] == "agent_output"
    rows = [e for e in log.read() if e["agent_id"] == "step:p/s"]
    call_idx = next(i for i, e in enumerate(rows)
                    if e["type"] == "tool_call" and e["payload"]["id"] == "t1")
    res_idx = next(i for i, e in enumerate(rows)
                   if e["type"] == "tool_result" and e["payload"]["id"] == "t1")
    assert call_idx < res_idx
    assert json.loads(rows[res_idx]["payload"]["content"])["echo"] == "only"


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
@pytest.mark.anyio
async def test_two_task_turn_both_results_land(
    tmp_path: Path, provider: str, provider_registry,
) -> None:
    """AC2 (both loops): two task tus in ONE assistant message both produce
    tool_result events and the run completes — the provider loop fans them out
    through the batch."""
    (tmp_path / "art").mkdir()
    reset_parallelism_cache()
    responses = {
        "anthropic": [
            response(
                tool_use("t1", "task",
                         {"subagent_name": "reviewer", "task_description": "first"}),
                tool_use("t2", "task",
                         {"subagent_name": "reviewer", "task_description": "second"})),
            response(tool_use("pc", "complete_step", {"status": "Success"})),
        ],
        "openai": [
            resp_response(
                resp_function_call(
                    "t1", "task",
                    '{"subagent_name": "reviewer", "task_description": "first"}'),
                resp_function_call(
                    "t2", "task",
                    '{"subagent_name": "reviewer", "task_description": "second"}')),
            resp_response(resp_function_call(
                "pc", "complete_step", '{"status": "Success"}')),
        ],
    }
    runner, (pname, pfactory) = _make(provider, tmp_path, responses)
    provider_registry(pname, env_check=lambda: True, factory=pfactory)
    provider_registry("rec", env_check=lambda: True,
                      factory=lambda m: _EchoProvider("rec", m))
    log = InMemoryEventLog()
    events = [e async for e in runner.run(_inv(tmp_path, log, provider))]
    reset_parallelism_cache()

    assert events[-1]["type"] == "agent_output"
    results = {e["payload"]["id"]: e for e in log.read()
               if e["type"] == "tool_result" and e["agent_id"] == "step:p/s"}
    assert set(results) == {"t1", "t2"}
    assert json.loads(results["t1"]["payload"]["content"])["echo"] == "first"
    assert json.loads(results["t2"]["payload"]["content"])["echo"] == "second"


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
@pytest.mark.anyio
async def test_task_tu_does_not_route_through_dispatch_one_tool_use(
    tmp_path: Path, provider: str, provider_registry,
) -> None:
    """Wiring guard: task tus route through dispatch_task_batch (via
    dispatch_turn_tool_uses), NOT through dispatch_one_tool_use. A patch that
    makes dispatch_one_tool_use raise for task tus verifies the batch path owns
    them; the old per-tu loop would fail this test."""
    import imp.agents.dispatch as dispatch_mod

    original_one = dispatch_mod.dispatch_one_tool_use

    async def _guard_task_tus(**kwargs) -> object:
        tu = kwargs["tu"]
        if tu.name == "task":
            raise RuntimeError(
                "task tu reached dispatch_one_tool_use — wiring broken")
        return await original_one(**kwargs)

    (tmp_path / "art").mkdir()
    reset_parallelism_cache()
    responses = {
        "anthropic": [
            response(
                tool_use("t1", "task",
                         {"subagent_name": "reviewer", "task_description": "one"}),
                tool_use("t2", "task",
                         {"subagent_name": "reviewer", "task_description": "two"})),
            response(tool_use("pc", "complete_step", {"status": "Success"})),
        ],
        "openai": [
            resp_response(
                resp_function_call(
                    "t1", "task",
                    '{"subagent_name": "reviewer", "task_description": "one"}'),
                resp_function_call(
                    "t2", "task",
                    '{"subagent_name": "reviewer", "task_description": "two"}')),
            resp_response(resp_function_call(
                "pc", "complete_step", '{"status": "Success"}')),
        ],
    }
    runner, (pname, pfactory) = _make(provider, tmp_path, responses)
    provider_registry(pname, env_check=lambda: True, factory=pfactory)
    provider_registry("rec", env_check=lambda: True,
                      factory=lambda m: _EchoProvider("rec", m))
    log = InMemoryEventLog()
    dispatch_mod.dispatch_one_tool_use = _guard_task_tus  # type: ignore[assignment]
    try:
        events = [e async for e in runner.run(_inv(tmp_path, log, provider))]
    finally:
        dispatch_mod.dispatch_one_tool_use = original_one  # type: ignore[assignment]
        reset_parallelism_cache()
    # If dispatch_one_tool_use was called for a task tu, _guard_task_tus would
    # raise RuntimeError inside the provider loop. The fact that the run
    # completes proves the batch path owns task tus end-to-end.
    assert events[-1]["type"] == "agent_output"
    results = {e["payload"]["id"]: e for e in log.read()
               if e["type"] == "tool_result" and e["agent_id"] == "step:p/s"}
    assert set(results) == {"t1", "t2"}
