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
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        provider_registry) -> None:
    """A subagent backoff respawn emits one agent_retry under the subagent's agent_id."""
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

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

    provider_registry("fake", lambda: True, lambda _m: _SubProvider())

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
async def test_clean_subagent_emits_no_agent_retry(
        tmp_path: Path, provider_registry) -> None:
    """A subagent that succeeds on its first drive emits no agent_retry event."""
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

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

    provider_registry("fake", lambda: True, lambda _m: _SubProvider())

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
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        provider_registry) -> None:
    """Attempt 1 fails `network` (opens the window), attempt 2 fails `unknown`:
    the subagent keeps retrying (a second agent_retry under sub:) instead of
    handing back after the single retry."""
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

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

    provider_registry("fake", lambda: True, lambda _m: _SubProvider())

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
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        provider_registry) -> None:
    """A mid-transient teardown tagged kind='interrupted' on a sticky category
    never auto-retries: the pre-classification interrupt guard returns before
    decide, so only the first (network) retry is emitted."""
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

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

    provider_registry("fake", lambda: True, lambda _m: _SubProvider())

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


@pytest.mark.anyio
async def test_subagent_failed_handback_emits_audit_error(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        provider_registry) -> None:
    """A subagent whose drive fails a hard-stop category (config) reaches the
    failed/fatal handback, which appends exactly one audit `error` event under
    the subagent's agent_id carrying category/kind/message — queryable via the
    same filter `imp events --type error --agent sub:<id>` uses."""
    from imp.engine import events_index as ei
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

    async def _no_sleep(_delay: float, _coordinator: Any) -> None:
        return None
    monkeypatch.setattr("imp.agents.subagents._interruptible_sleep", _no_sleep)

    class _ConfigFailRunner:
        async def run(self, inv):
            from imp.agents.transcript import Transcript
            Transcript(inv.transcript_dir / "transcript.jsonl").append(
                Message(role="user", content=(TextBlock(text="work"),)))
            yield {"type": "error", "payload": {
                "category": "config", "kind": "config", "message": "bad config"}}

        async def interrupt(self) -> None:
            ...

    class _SubProvider:
        name = "fake"
        model = "m"

        def create_runner(self, _inv):
            return _ConfigFailRunner()

    provider_registry("fake", lambda: True, lambda _m: _SubProvider())

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
    audit = [e for e in events
             if e["type"] == "error" and e["payload"].get("audit")]
    assert len(audit) == 1
    ev = audit[0]
    assert ev["agent_id"].startswith("sub:")
    assert ev["payload"]["category"] == "config"
    assert ev["payload"]["kind"] == "config"
    assert f"failed ({ev['payload']['category']})" in ev["payload"]["message"]
    # Queryable through the exact filter path `imp events --type error --agent` runs.
    indexed = list(enumerate(events, start=1))
    handle_map = ei.subagent_handle_map(indexed)
    selected = ei.select_event_lines(
        indexed, types={"error"}, agents={ev["agent_id"]}, handle_map=handle_map)
    assert any(e["payload"].get("audit") for _, e in selected)


@pytest.mark.anyio
async def test_subagent_budget_exhaustion_emits_audit_error(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        provider_registry) -> None:
    """A persistent network flap that exceeds the backoff budget hands back via
    the budget-exhaustion path, which appends exactly one audit `error` event
    under the subagent's agent_id carrying the flapping category and a
    ceiling-marking kind/message. The single retry before exhaustion still emits
    its agent_retry."""
    import imp.engine.error_policy as ep
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

    async def _no_sleep(_delay: float, _coordinator: Any) -> None:
        return None
    monkeypatch.setattr("imp.agents.subagents._interruptible_sleep", _no_sleep)
    # Admit the first 1.0s network retry (cum 1.0) then cap the 2.0s second delay
    # (1.0 + 2.0 = 3.0 > 1.5) so the flap exhausts the budget on drive 2.
    monkeypatch.setattr(ep, "budget_for", lambda category: 1.5)

    class _NetworkFlapRunner:
        async def run(self, inv):
            from imp.agents.transcript import Transcript
            Transcript(inv.transcript_dir / "transcript.jsonl").append(
                Message(role="user", content=(TextBlock(text="work"),)))
            yield {"type": "error", "payload": {
                "category": "network", "kind": "network",
                "message": "connection refused"}}

        async def interrupt(self) -> None:
            ...

    class _SubProvider:
        name = "fake"
        model = "m"

        def create_runner(self, _inv):
            return _NetworkFlapRunner()

    provider_registry("fake", lambda: True, lambda _m: _SubProvider())

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
    audit = [e for e in events
             if e["type"] == "error" and e["payload"].get("audit")]
    assert len(audit) == 1
    ev = audit[0]
    assert ev["agent_id"].startswith("sub:")
    assert ev["payload"]["category"] == "network"
    # kind/message mark ceiling exhaustion (distinct from a per-attempt retry).
    assert ev["payload"]["kind"] == "budget_exhausted"
    assert "budget" in ev["payload"]["message"]
    # The single retry before exhaustion still emitted its agent_retry.
    retries = [e for e in events if e["type"] == "agent_retry"]
    assert [r["payload"]["category"] for r in retries] == ["network"]


@pytest.mark.anyio
async def test_subagent_interrupted_pause_emits_no_audit_error(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        provider_registry) -> None:
    """A human-pause handback (interrupted teardown) appends NO audit error: a
    pause is not an error give-up, and result.error there is a stale retryable
    category."""
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

    async def _no_sleep(_delay: float, _coordinator: Any) -> None:
        return None
    monkeypatch.setattr("imp.agents.subagents._interruptible_sleep", _no_sleep)

    class _InterruptedTeardownRunner:
        async def run(self, inv):
            from imp.agents.transcript import Transcript
            Transcript(inv.transcript_dir / "transcript.jsonl").append(
                Message(role="user", content=(TextBlock(text="work"),)))
            # kind == "interrupted" -> drive returns paused=True (interrupt=True),
            # so the loop breaks into the result.paused suspend handback.
            yield {"type": "error", "payload": {
                "category": "network", "kind": "interrupted",
                "message": "torn down mid-outage"}}

        async def interrupt(self) -> None:
            ...

    class _SubProvider:
        name = "fake"
        model = "m"

        def create_runner(self, _inv):
            return _InterruptedTeardownRunner()

    provider_registry("fake", lambda: True, lambda _m: _SubProvider())

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
    assert not any(e["type"] == "error" and e["payload"].get("audit")
                   for e in events)


@pytest.mark.anyio
async def test_subagent_context_ceiling_emits_no_audit_error(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        provider_registry) -> None:
    """A context-ceiling handback appends NO audit error: its terminal state is
    already captured by the [context_exhausted] marker."""
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

    async def _no_sleep(_delay: float, _coordinator: Any) -> None:
        return None
    monkeypatch.setattr("imp.agents.subagents._interruptible_sleep", _no_sleep)

    class _ContextWindowRunner:
        async def run(self, inv):
            from imp.agents.transcript import Transcript
            Transcript(inv.transcript_dir / "transcript.jsonl").append(
                Message(role="user", content=(TextBlock(text="work"),)))
            # context_window Pauses -> fatal result; the subagent loop routes it to
            # the context-ceiling handback (fatal and category == "context_window").
            yield {"type": "error", "payload": {
                "category": "context_window", "kind": "context_window",
                "message": "context limit reached"}}

        async def interrupt(self) -> None:
            ...

    class _SubProvider:
        name = "fake"
        model = "m"

        def create_runner(self, _inv):
            return _ContextWindowRunner()

    provider_registry("fake", lambda: True, lambda _m: _SubProvider())

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
    assert not any(e["type"] == "error" and e["payload"].get("audit")
                   for e in events)


@pytest.mark.anyio
async def test_subagent_backoff_ended_clears_top_level_countdown(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        provider_registry) -> None:
    """A subagent's backoff_ended routed into its fold drops the top-level countdown_total."""
    from imp.cli import narrative as nv
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

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

    provider_registry("fake", lambda: True, lambda _m: _SubProvider())

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
    retry = next(e for e in events if e["type"] == "agent_retry")
    ended = next(e for e in events if e["type"] == "backoff_ended")
    assert ended["agent_id"] == retry["agent_id"]        # same subagent scope
    event_id = retry["agent_id"].removeprefix("sub:")

    b = nv.NarrativeBuilder()
    b.add({"id": "q", "agent_id": None, "type": "quest_created",
           "payload": {"quest_id": 1, "title": "x"}})
    b.add({"id": "s", "agent_id": None, "type": "step_started",
           "payload": {"step": "impl", "kind": "agent", "phase": "impl",
                       "phase_label": "implementation phase",
                       "label": "implementing", "model": "m"}})
    # Registers the fold under sub:<event_id> — the id the real events carry.
    b.add({"id": event_id, "agent_id": "step:impl/lead", "type": "tool_call",
           "payload": {"id": event_id, "name": "sub_agent",
                       "input": {"subagent_name": "coder",
                                 "task_description": "edit"}}})

    b.add(retry)
    assert b.current_activity().countdown_total == retry["payload"]["backoff_seconds"]
    b.add(ended)
    assert b.current_activity().countdown_total is None
