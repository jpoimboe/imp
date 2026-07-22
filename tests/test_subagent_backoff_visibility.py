from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
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
        quest_state=State(id="c", step="lead", step_attempt=0,
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
                yield {"type": "agent_result",
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
            yield {"type": "agent_result",
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


@pytest.mark.anyio
async def test_subagent_sticky_retry_rides_out_unknown(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Attempt 1 fails `network` (opens the window), attempt 2 fails `unknown`:
    the subagent keeps retrying (a second agent_retry under sub:) instead of
    handing back after the single retry."""
    from imp.providers import resolve
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

    resolve.reset_providers_for_tests()

    async def _no_sleep(_delay: float, _coordinator: Any) -> None:
        return None
    monkeypatch.setattr("imp.agents.subagents._interruptible_sleep", _no_sleep)

    class _ScriptedRunner:
        def __init__(self, category: str | None) -> None:
            self.category = category

        async def run(self, inv):
            from imp.agents.transcript import Transcript
            Transcript(inv.transcript_dir / "transcript.jsonl").append(
                Message(role="user", content=(TextBlock(text="work"),)))
            if self.category is not None:
                yield {"type": "error", "payload": {
                    "category": self.category, "kind": self.category,
                    "message": f"{self.category} blip"}}
            else:
                yield {"type": "agent_result",
                       "payload": {"status": "Success", "outcome": "done"}}

        async def interrupt(self) -> None:
            ...

    script = ["network", "unknown", None]
    spawns = {"n": 0}

    class _SubProvider:
        name = "fake"
        model = "m"

        def create_runner(self, _inv):
            i = spawns["n"]
            spawns["n"] += 1
            return _ScriptedRunner(script[i])

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
    assert [r["payload"]["category"] for r in retries] == ["network", "unknown"]
    assert [r["payload"]["attempt"] for r in retries] == [1, 2]
    assert all(r["agent_id"].startswith("sub:") for r in retries)


@pytest.mark.anyio
async def test_subagent_interrupted_teardown_is_not_sticky_retried(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mid-transient teardown tagged kind='interrupted' on a sticky category
    never auto-retries: the pre-classification interrupt guard returns before
    decide, so only the first (network) retry is emitted."""
    from imp.providers import resolve
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

    resolve.reset_providers_for_tests()

    async def _no_sleep(_delay: float, _coordinator: Any) -> None:
        return None
    monkeypatch.setattr("imp.agents.subagents._interruptible_sleep", _no_sleep)

    class _ScriptedRunner:
        def __init__(self, spec) -> None:
            self.spec = spec   # (category, kind) or None

        async def run(self, inv):
            from imp.agents.transcript import Transcript
            Transcript(inv.transcript_dir / "transcript.jsonl").append(
                Message(role="user", content=(TextBlock(text="work"),)))
            if self.spec is not None:
                category, kind = self.spec
                yield {"type": "error", "payload": {
                    "category": category, "kind": kind,
                    "message": f"{category}/{kind}"}}
            else:
                yield {"type": "agent_result",
                       "payload": {"status": "Success", "outcome": "done"}}

        async def interrupt(self) -> None:
            ...

    # spawn 0 opens the window (network retry); spawn 1 is an interrupted teardown
    # tagged as the sticky `unknown` category — the guard must stop it retrying.
    # Trailing success entry: if the interrupt guard regressed and the sticky
    # `unknown` were retried, the third spawn returns cleanly so the retry-count
    # assertion fails on a clean assertion rather than a script IndexError.
    script = [("network", "network"), ("unknown", "interrupted"), None]
    spawns = {"n": 0}

    class _SubProvider:
        name = "fake"
        model = "m"

        def create_runner(self, _inv):
            i = spawns["n"]
            spawns["n"] += 1
            return _ScriptedRunner(script[i])

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
    # Only the network retry; the interrupted `unknown` teardown is not retried.
    assert [r["payload"]["category"] for r in retries] == ["network"]
    assert all(r["agent_id"].startswith("sub:") for r in retries)
