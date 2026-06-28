from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
from imp.engine.state import State
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc


@pytest.fixture
def anyio_backend():
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


@pytest.mark.anyio
async def test_subagent_reincarnates_within_dispatch_then_is_error_at_cap(
        tmp_path: Path) -> None:
    """A subagent that hits the hard window error is reborn *inside* its own
    dispatch (the parent sees one resolved sub_agent call), up to the per-session
    cap; after the cap it degrades to the parent's is_error tool_result. One
    `reincarnating` event is emitted and the prior subagent transcript is stashed."""
    from imp.providers import resolve
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

    resolve.reset_providers_for_tests()

    class _CtxWindowSubRunner:
        async def run(self, inv):
            from imp.agents.transcript import Transcript
            Transcript(inv.transcript_dir / "transcript.jsonl").append(
                Message(role="user", content=(TextBlock(text="work"),)))
            yield {"type": "error", "payload": {
                "category": "context_window", "kind": "prompt_too_long",
                "message": "prompt is too long"}}

        async def interrupt(self) -> None:
            ...

    class _SubProvider:
        name = "fake"
        model = "m"

        def create_runner(self, _inv):
            return _CtxWindowSubRunner()

    resolve.register_provider("fake", env_check=lambda: True,
                              factory=lambda _m: _SubProvider())

    # Parent: a real anthropic runner that calls `sub_agent` then completes.
    fake = FakeAsyncAnthropic([
        response(tool_use("t1", "sub_agent",
                          {"subagent_name": "coder",
                           "task_description": "edit"})),
        response(tool_use("c1", "complete_step",
                          {"status": "Success", "outcome": "done"})),
    ])
    runner = AnthropicAgentRunner(model_name="m", client_factory=lambda: fake)

    inv = AgentInvocation(
        agent_def=_agent("lead", tools={"complete_step", "sub_agent"},
                         subagents=("coder",)),
        provider_name="fake", model_name="m",          # subagent inherits -> fake
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(title="T", project_root=tmp_path,
                                     workflow_dir=tmp_path / "wf"),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        subagent_defs={"coder": _agent("coder", tools={"complete_step"})},
        agent_id="step:impl/lead",
        change_state=State(id="c", step="lead", step_attempt=0,
                           status="running", max_reincarnations=1))

    async for _ in runner.run(inv):
        pass

    events = [json.loads(line)
              for line in inv.events_path.read_text().splitlines() if line]
    reincarnating = [e for e in events if e["type"] == "reincarnating"]
    assert len(reincarnating) == 1
    assert reincarnating[0]["payload"]["reason"] == "context window exceeded"
    assert (tmp_path / "step" / "subagents" / "coder-1.attempt-1").exists()
    assert (tmp_path / "step" / "subagents" / "coder-2").exists()
    is_error = [
        e for e in events
        if e["type"] == "tool_result"
        and e["payload"].get("error") is True
        and "context_window" in (e["payload"].get("content") or "")
    ]
    assert is_error, "expected the post-cap subagent failure as an is_error tool_result"


@pytest.mark.anyio
async def test_subagent_reincarnates_on_handoff_then_is_error_at_cap(
        tmp_path: Path) -> None:
    """A subagent that captures a Tier A handoff is reborn INSIDE its own dispatch
    (the parent sees one resolved sub_agent call), up to the per-session cap;
    after the cap it degrades to the parent's is_error tool_result. One
    `reincarnating` event with the threshold reason is emitted and the prior
    subagent transcript is stashed."""
    from imp.providers import resolve
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

    resolve.reset_providers_for_tests()

    class _HandoffSubRunner:
        async def run(self, inv):
            from imp.agents.transcript import Transcript
            from imp.engine.reincarnation import handoff_path_for
            Transcript(inv.transcript_dir / "transcript.jsonl").append(
                Message(role="user", content=(TextBlock(text="work"),)))
            hp = handoff_path_for(inv.transcript_dir)
            hp.write_text("# handoff\n")
            yield {"type": "handoff", "payload": {"handoff_path": str(hp)}}

        async def interrupt(self):
            ...

    seen_thresholds: list = []

    class _SubProvider:
        name = "fake"
        model = "m"

        def create_runner(self, _inv):
            # Falsifies the parent->subagent threshold propagation (Steps 3-5):
            # the subagent invocation must carry the run's checkpoint_threshold.
            seen_thresholds.append(_inv.checkpoint_threshold)
            return _HandoffSubRunner()

    resolve.register_provider("fake", env_check=lambda: True,
                              factory=lambda _m: _SubProvider())

    fake = FakeAsyncAnthropic([
        response(tool_use("t1", "sub_agent",
                          {"subagent_name": "coder", "task_description": "edit"})),
        response(tool_use("c1", "complete_step",
                          {"status": "Success", "outcome": "done"})),
    ])
    runner = AnthropicAgentRunner(model_name="m", client_factory=lambda: fake)

    inv = AgentInvocation(
        agent_def=_agent("lead", tools={"complete_step", "sub_agent"},
                         subagents=("coder",)),
        provider_name="fake", model_name="m",          # subagent inherits -> fake
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(title="T", project_root=tmp_path,
                                     workflow_dir=tmp_path / "wf"),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        subagent_defs={"coder": _agent("coder", tools={"complete_step"})},
        agent_id="step:impl/lead",
        checkpoint_threshold=0.8,
        change_state=State(id="c", step="lead", step_attempt=0,
                           status="running", max_reincarnations=1))

    async for _ in runner.run(inv):
        pass

    # The run's threshold reached each (re)dispatched subagent invocation.
    assert seen_thresholds and all(t == 0.8 for t in seen_thresholds)
    events = [json.loads(line)
              for line in inv.events_path.read_text().splitlines() if line]
    reincarnating = [e for e in events if e["type"] == "reincarnating"]
    assert len(reincarnating) == 1
    assert reincarnating[0]["payload"]["reason"] == "context threshold crossed"
    assert (tmp_path / "step" / "subagents" / "coder-1.attempt-1").exists()
    assert (tmp_path / "step" / "subagents" / "coder-2").exists()
    is_error = [
        e for e in events
        if e["type"] == "tool_result" and e["payload"].get("error") is True
    ]
    assert is_error, "expected the post-cap subagent failure as an is_error tool_result"
