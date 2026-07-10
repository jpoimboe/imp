from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, QuestContext, Message, TextBlock
from imp.engine.state import State
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _agent(name: str, *, tools, subagents=()) -> AgentDef:
    return AgentDef(
        name=name, description="", tools=frozenset(tools),
        subagents=tuple(subagents),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="go", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1))


def _lead_inv(tmp_path: Path, fake: Any) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent("lead", tools={"complete_step", "sub_agent"},
                         subagents=("coder",)),
        provider_name="fake", model_name="m",          # subagent inherits -> fake
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(title="T", project_root=tmp_path,
                                     workflow_dir=tmp_path / "wf"),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        subagent_defs={"coder": _agent("coder", tools={"complete_step"})},
        agent_id="step:impl/lead",
        change_state=State(id="c", step="lead", step_attempt=0,
                           status="running", max_reincarnations=1))


@pytest.mark.anyio
async def test_subagent_backoff_emits_agent_retry(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A subagent backoff respawn emits one agent_retry under the subagent's agent_id."""
    from imp.providers import resolve
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

    resolve.reset_providers_for_tests()

    async def _no_sleep(_delay: float, _coordinator: Any) -> None:
        return None
    monkeypatch.setattr("imp.agents.subagents._interruptible_sleep", _no_sleep)

    class _RateLimitThenOkRunner:
        def __init__(self, fail: bool) -> None:
            self.fail = fail

        async def run(self, inv):
            from imp.agents.transcript import Transcript
            Transcript(inv.transcript_dir / "transcript.jsonl").append(
                Message(role="user", content=(TextBlock(text="work"),)))
            if self.fail:
                yield {"type": "error", "payload": {
                    "category": "rate_limit", "kind": "rate_limit",
                    "message": "429 rate limited"}}
            else:
                yield {"type": "agent_output",
                       "payload": {"status": "Success", "outcome": "done"}}

        async def interrupt(self) -> None:
            ...

    spawns = {"n": 0}

    class _SubProvider:
        name = "fake"
        model = "m"

        def create_runner(self, _inv):
            i = spawns["n"]
            spawns["n"] += 1
            return _RateLimitThenOkRunner(fail=(i == 0))

    resolve.register_provider("fake", env_check=lambda: True,
                              factory=lambda _m: _SubProvider())

    fake = FakeAsyncAnthropic([
        response(tool_use("t1", "sub_agent",
                          {"subagent_name": "coder", "task_description": "edit"})),
        response(tool_use("c1", "complete_step",
                          {"status": "Success", "outcome": "done"})),
    ])
    runner = AnthropicAgentRunner(model_name="m", client_factory=lambda: fake)

    async for _ in runner.run(_lead_inv(tmp_path, fake)):
        pass

    events = [json.loads(line)
              for line in (tmp_path / "events.jsonl").read_text().splitlines()
              if line]
    retries = [e for e in events if e["type"] == "agent_retry"]
    assert len(retries) == 1
    assert retries[0]["payload"]["category"] == "rate_limit"
    assert retries[0]["payload"]["attempt"] == 1
    # error_policy.decide for prior_backoff_attempts=0 yields min(60, max(2**0, 0)) = 1.0.
    assert retries[0]["payload"]["backoff_seconds"] == 1.0
    # Folds under the subagent, not the parent step.
    assert retries[0]["agent_id"].startswith("sub:")


@pytest.mark.anyio
async def test_clean_subagent_emits_no_agent_retry(tmp_path: Path) -> None:
    """A subagent that succeeds on its first drive emits no agent_retry event."""
    from imp.providers import resolve
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

    resolve.reset_providers_for_tests()

    class _OkRunner:
        async def run(self, inv):
            from imp.agents.transcript import Transcript
            Transcript(inv.transcript_dir / "transcript.jsonl").append(
                Message(role="user", content=(TextBlock(text="work"),)))
            yield {"type": "agent_output",
                   "payload": {"status": "Success", "outcome": "done"}}

        async def interrupt(self) -> None:
            ...

    class _SubProvider:
        name = "fake"
        model = "m"

        def create_runner(self, _inv):
            return _OkRunner()

    resolve.register_provider("fake", env_check=lambda: True,
                              factory=lambda _m: _SubProvider())

    fake = FakeAsyncAnthropic([
        response(tool_use("t1", "sub_agent",
                          {"subagent_name": "coder", "task_description": "edit"})),
        response(tool_use("c1", "complete_step",
                          {"status": "Success", "outcome": "done"})),
    ])
    runner = AnthropicAgentRunner(model_name="m", client_factory=lambda: fake)

    async for _ in runner.run(_lead_inv(tmp_path, fake)):
        pass

    events = [json.loads(line)
              for line in (tmp_path / "events.jsonl").read_text().splitlines()
              if line]
    assert not any(e["type"] == "agent_retry" for e in events)
