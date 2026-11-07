from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

import tests.anthropic_fakes as af
import tests.cc_fakes as cc
import tests.codex_fakes as cx
from imp.agents.agent_loop import run_agent_loop
from imp.agents.loop_items import SaveRequested
from imp.agents.parallel import reset_parallelism_cache
from imp.agents.tools.save_and_exit import save_and_exit
from imp.cli.signals import InterruptCoordinator
from imp.engine.abandon import save_as_draft
from imp.engine.quest import Quest
from imp.engine.state import InMemoryEventLog, State
from imp.providers.anthropic import AnthropicAgentRunner, AnthropicSession
from imp.providers.claude import ClaudeAgentRunner, ClaudeSession
from imp.providers.codex_runner import CodexAgentRunner, CodexSession
from imp.providers.observe_session import ObserveSession
from tests.async_wait import HANG_BACKSTOP
from tests.conftest import _seed_control
from tests.test_anthropic_runner import _inv as _anthropic_inv
from tests.test_claude_runner import _inv as _claude_inv
from tests.test_codex_runner_happy_path import _build_invocation as _codex_inv
from tests.test_codex_runner_happy_path import _seed_auth


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _SaveHook:
    """Records each save request and cancels the run, as CONTROL_SAVE does."""

    def __init__(self) -> None:
        self.calls: list[str | None] = []
        self.scope = anyio.CancelScope()

    def __call__(self, reason: str | None) -> None:
        self.calls.append(reason)
        self.scope.cancel()


def _coordinator(hook: _SaveHook) -> InterruptCoordinator:
    coord = InterruptCoordinator()
    coord.set_save_hook(hook)
    return coord


PAUSED = "Paused. The session closes at the end of this turn."


def _draft(tmp_path: Path) -> Quest:
    _seed_control(tmp_path)
    return Quest.create_draft(tmp_path, title="X", quest_id="sv1", base_branch="main")


def _log(quest: Quest) -> list[dict[str, Any]]:
    return list(quest.event_log().read())


@dataclass
class _Saved:
    calls: list[str | None]
    yielded: list[dict[str, Any]]
    continued: list[str]


async def _run_until_saved(runner: Any, inv: Any, quest: Quest) -> _Saved:
    """Run a top-level step until the save hook cancels it, then land the daemon's save."""
    hook = _SaveHook()
    continued: list[str] = []

    def _continue(last_text: str) -> tuple[str, bool]:
        continued.append(last_text)
        hook.scope.cancel()  # a missed checkpoint fails fast instead of hanging
        return ("keep going", False)

    inv = replace(inv, quest=quest, quest_state=State.load(quest.state_dir),
                  coordinator=_coordinator(hook), event_log=quest.event_log(),
                  events_path=quest.events_path, continuation_fn=_continue)
    yielded: list[dict[str, Any]] = []
    with anyio.fail_after(HANG_BACKSTOP), hook.scope:
        async for ev in runner.run(inv):
            yielded.append(ev)
    assert hook.scope.cancelled_caught
    save_as_draft(quest)
    return _Saved(hook.calls,
                  [e for e in yielded if e["type"] != "tool_heartbeat"], continued)


def _assert_saved_after_the_turn(quest: Quest, tool_use_id: str) -> None:
    events = _log(quest)
    types = [e["type"] for e in events]
    assert types.count("agent_message") == 1
    assert not any("LATER TURN" in json.dumps(e["payload"]) for e in events)
    call = next(i for i, e in enumerate(events)
                if e["type"] == "tool_call" and e["payload"]["id"] == tool_use_id)
    res = next(i for i, e in enumerate(events)
               if e["type"] == "tool_result" and e["payload"]["id"] == tool_use_id)
    assert events[call]["payload"]["name"] == "save_and_exit"
    assert events[res]["payload"]["content"] == PAUSED
    assert events[res]["payload"]["error"] is False
    assert types.index("agent_message") < call < res < types.index("conversation_saved")
    assert types[-1] == "conversation_saved"


def _raw_label(raw: Any) -> str:
    kind = getattr(raw, "type", None) or type(raw).__name__
    return f"{kind}:{getattr(getattr(raw, 'item', None), 'tool', '') or ''}"


def _trace_session(
    monkeypatch: pytest.MonkeyPatch, cls: type[ObserveSession], quest: Quest,
    *, body_before: str | None = None,
) -> list[str]:
    """Record each raw message the session consumes, and the log tail when it
    yields SaveRequested. `body_before` runs the real tool body once, first."""
    trace: list[str] = []
    real_items, real_events = cls._message_to_items, cls.events

    async def items(self: Any, raw: Any) -> Any:
        label = _raw_label(raw)
        if label == body_before and not any(t.startswith("body: ") for t in trace):
            trace.append(f"body: {await save_and_exit(self.ctx)}")
        trace.append(label)
        async with contextlib.aclosing(real_items(self, raw)) as agen:
            async for item in agen:
                yield item

    async def events(self: Any) -> Any:
        async with contextlib.aclosing(real_events(self)) as agen:
            async for item in agen:
                if isinstance(item, SaveRequested):
                    tail = [e["type"] for e in _log(quest)][-2:]
                    trace.append(f"SaveRequested after {','.join(tail)}")
                yield item

    monkeypatch.setattr(cls, "_message_to_items", items)
    monkeypatch.setattr(cls, "events", events)
    return trace


class _SavingObserve(ObserveSession):
    """Its first raw message logs a save_and_exit result; later output must stay unread."""

    def __init__(self, coordinator: Any) -> None:
        super().__init__(SimpleNamespace(), SimpleNamespace(_cancel=False))
        self.ctx = SimpleNamespace(coordinator=coordinator, dependency_wait=())
        self.consumed: list[str] = []

    @property
    def _save_result_logged(self) -> bool:
        return bool(self.consumed)

    async def _enter(self) -> None:
        pass

    async def _receive(self) -> Any:
        for raw in ("save_and_exit result", "later model output"):
            yield raw

    async def _message_to_items(self, raw: Any) -> Any:
        self.consumed.append(raw)
        return
        yield  # pragma: no cover — makes this an async generator

    def _fatal_error(self) -> dict[str, Any] | None:
        return None

    async def _is_terminal(self, raw: Any) -> bool:
        raise AssertionError("the save checkpoint must precede the terminal check")


@pytest.mark.anyio
async def test_observe_events_stop_at_the_logged_save_result() -> None:
    session = _SavingObserve(coordinator=None)
    items = [item async for item in session.events()]
    assert items == [SaveRequested()]
    assert session.consumed == ["save_and_exit result"]


@pytest.mark.anyio
async def test_observe_checkpoint_requests_one_save_then_parks() -> None:
    hook = _SaveHook()
    session = _SavingObserve(_coordinator(hook))
    continued: list[str] = []

    def _continue(last_text: str) -> tuple[str, bool]:
        continued.append(last_text)
        hook.scope.cancel()  # a missed checkpoint fails fast instead of hanging
        return ("keep going", False)

    yielded: list[dict[str, Any]] = []
    with anyio.fail_after(HANG_BACKSTOP), hook.scope:
        async for ev in run_agent_loop(
                SimpleNamespace(continuation_fn=_continue), session):
            yielded.append(ev)
    assert hook.scope.cancelled_caught
    assert hook.calls == [None]
    assert yielded == []
    assert continued == []


@pytest.mark.anyio
async def test_claude_saves_after_the_logged_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _draft(tmp_path)
    trace = _trace_session(monkeypatch, ClaudeSession, quest,
                           body_before="AssistantMessage:")
    # The fake bypasses the tool host, so it scripts the tool's result text.
    scripted = [
        cc.assistant(cc.text("setting this aside"),
                     cc.tool_use("s1", "save_and_exit", {})),
        cc.user(cc.tool_result("s1", PAUSED)),
        cc.assistant(cc.text("LATER TURN")),
        cc.result(),
    ]
    runner = ClaudeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=lambda opts: cc.FakeClaudeSDKClient(
            opts, scripted_messages=scripted))
    inv = _claude_inv(tmp_path, tools=frozenset({"save_and_exit", "complete_step"}))
    saved = await _run_until_saved(runner, inv, quest)
    assert trace == [f"body: {PAUSED}", "AssistantMessage:", "UserMessage:",
                     "SaveRequested after tool_call,tool_result"]
    assert saved.calls == [None]
    assert saved.yielded == []
    assert saved.continued == []
    _assert_saved_after_the_turn(quest, "s1")


@pytest.mark.anyio
async def test_claude_flagged_save_result_does_not_checkpoint(tmp_path: Path) -> None:
    hook = _SaveHook()
    scripted = [
        cc.assistant(cc.tool_use("s1", "save_and_exit", {})),
        cc.user(cc.tool_result("s1", "RuntimeError: boom", is_error=True)),
        cc.assistant(cc.tool_use("c1", "complete_step", {"status": "Success"})),
        cc.result(),
    ]
    runner = ClaudeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=lambda opts: cc.FakeClaudeSDKClient(
            opts, scripted_messages=scripted))
    inv = replace(_claude_inv(tmp_path, tools=frozenset({"save_and_exit", "complete_step"})),
                  coordinator=_coordinator(hook))
    # hook.scope: a regressed is_error guard fails fast instead of parking until the backstop.
    with anyio.fail_after(HANG_BACKSTOP), hook.scope:
        events = [ev async for ev in runner.run(inv)]
    assert hook.calls == []
    assert events[-1]["type"] == "agent_result"


@pytest.mark.anyio
async def test_codex_saves_after_the_logged_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _draft(tmp_path)
    _seed_auth(tmp_path)
    trace = _trace_session(monkeypatch, CodexSession, quest,
                           body_before="item.started:save_and_exit")
    # The fake bypasses the tool host, so it scripts the tool's result text.
    scripted = [[
        cx.thread_started("thr-1"),
        cx.agent_message("setting this aside"),
        cx.mcp_started("save_and_exit", item_id="s1"),
        cx.mcp_call(name="save_and_exit", result=PAUSED, item_id="s1"),
        cx.agent_message("LATER TURN"),
        cx.turn_completed(input_tokens=100, output_tokens=50),
    ]]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: cx.FakeCodex(scripted_turns=scripted))
    runner._canonical_auth_override = tmp_path / "auth.json"
    saved = await _run_until_saved(runner, _codex_inv(tmp_path), quest)
    assert trace.index(f"body: {PAUSED}") + 1 == trace.index("item.started:save_and_exit")
    assert trace[-2:] == ["item.completed:save_and_exit",
                          "SaveRequested after tool_call,tool_result"]
    assert saved.calls == [None]
    assert saved.yielded == []
    assert saved.continued == []
    _assert_saved_after_the_turn(quest, "s1")
    # The MCP server shut down gracefully despite the cancelled run.
    server = runner._server_handle.task
    assert server.done() and not server.cancelled()


@pytest.fixture(params=["partitioned", "kill_switch"])
def dispatch_branch(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> Any:
    if request.param == "kill_switch":
        monkeypatch.setenv("IMP_MAX_PARALLEL_TASKS", "1")
    else:
        monkeypatch.delenv("IMP_MAX_PARALLEL_TASKS", raising=False)
    reset_parallelism_cache()
    yield request.param
    reset_parallelism_cache()


def _save_then_complete() -> af.FakeAsyncAnthropic:
    return af.FakeAsyncAnthropic([
        af.response(af.text("setting this aside"),
                    af.tool_use("s1", "save_and_exit", {})),
        af.response(af.text("LATER TURN"),
                    af.tool_use("c1", "complete_step", {"status": "Success"})),
    ])


@pytest.mark.anyio
async def test_push_saves_after_the_dispatched_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dispatch_branch: str,
) -> None:
    quest = _draft(tmp_path)
    sent: list[Any] = []
    monkeypatch.setattr(AnthropicSession, "send_tool_results",
                        lambda self, results: sent.append(results))
    fake = _save_then_complete()
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=lambda: fake)
    inv = _anthropic_inv(tmp_path, tools=frozenset({"save_and_exit", "complete_step"}))
    saved = await _run_until_saved(runner, inv, quest)
    assert saved.calls == [None]
    assert saved.yielded == []
    assert sent == []
    assert saved.continued == []
    assert len(fake.messages.calls) == 1
    _assert_saved_after_the_turn(quest, "s1")


@pytest.mark.anyio
async def test_push_errored_save_result_does_not_save(
    tmp_path: Path, dispatch_branch: str,
) -> None:
    hook = _SaveHook()
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6",
                                  client_factory=_save_then_complete)
    # No quest_state: the tool's top-level-step guard raises, an error result.
    inv = replace(
        _anthropic_inv(tmp_path, tools=frozenset({"save_and_exit", "complete_step"}),
                       event_log=log),
        coordinator=_coordinator(hook))
    yielded: list[dict[str, Any]] = []
    # hook.scope: a regressed is_error guard fails fast instead of parking until the backstop.
    with anyio.fail_after(HANG_BACKSTOP), hook.scope:
        async for ev in runner.run(inv):
            yielded.append(ev)
    assert hook.calls == []
    assert [e["type"] for e in yielded if e["type"] != "tool_heartbeat"] == ["agent_result"]
    result = next(e for e in log.read()
                  if e["type"] == "tool_result" and e["payload"]["id"] == "s1")
    assert result["payload"]["error"] is True
