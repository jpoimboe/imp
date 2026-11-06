"""The turn-end check for a message lost as hidden thinking: when it fires and when it doesn't."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import Message, TextBlock
from imp.cli.signals import InterruptCoordinator
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.engine.completion import (
    INTERRUPT_STEER_HINT,
    build_continuation_fn,
    build_inline_solicit,
)
from imp.engine.feedback import COMPLETION_NUDGE, UNDELIVERED_MESSAGE_NUDGE
from imp.engine.state import EventLog, InMemoryEventLog, JsonlEventLog

AGENT = "step:p/s"
SUBAGENT = "step:p/s/sub"
THINK = {"type": "thinking", "thinking": "hidden"}
TOOL = {"type": "tool_use", "id": "u1", "name": "read_file", "input": {}}
TEXT = {"type": "text", "text": "visible"}
STUB = "I'll wait for your answer."
RESEND = "Which of the two readings did you mean?"
SAY = Message(role="user", content=(TextBlock(text="hello"),))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _respond(log: EventLog, *blocks: dict[str, Any], agent_id: str = AGENT,
             message_id: str | None = None) -> None:
    log.append_agent_message(agent_id=agent_id, provider="p", model="m",
                             raw={"content": list(blocks)}, usage={}, stop_reason=None,
                             message_id=message_id)


def _lost_then_reply(log: EventLog, reply: str = STUB) -> str:
    _respond(log, THINK, THINK, TOOL)
    _respond(log, {"type": "text", "text": reply})
    return reply


def _cont(log: EventLog | None, *, interactive: bool = True, has_schema: bool = True,
          **kw: Any) -> Callable[[str], Any]:
    return build_continuation_fn(event_log=log, agent_id=AGENT, interactive=interactive,
                                 has_schema=has_schema, **kw)


def _corrections(log: EventLog) -> list[dict[str, Any]]:
    return [e for e in log.read() if e["type"] == "engine_message"
            and e["payload"]["subtype"] == "undelivered_message"]


def _assert_parks_on(fn: Callable[[str], Any], text: str) -> None:
    with pytest.raises(AwaitingUserInput) as exc:
        fn(text)
    assert exc.value.prompt == text


def test_correction_is_the_pinned_text() -> None:
    assert UNDELIVERED_MESSAGE_NUDGE == (
        "Your previous message may not have reached the user: the response before your last "
        "reply contained no visible text. If it held anything they need, such as a question "
        "or options, send it again now in full, as plain text with no tool calls. Otherwise, "
        "repeat what you need from them in one line.")


def test_fires_with_the_human_false_route_and_the_existing_payload_shape() -> None:
    log = InMemoryEventLog()
    assert _cont(log)(_lost_then_reply(log)) == (UNDELIVERED_MESSAGE_NUDGE, False)
    [event] = _corrections(log)
    assert event["agent_id"] == AGENT
    assert event["payload"] == {
        "role": "user", "subtype": "undelivered_message",
        "content": [{"type": "text", "text": UNDELIVERED_MESSAGE_NUDGE}]}


@pytest.mark.parametrize(("length", "fires"), [(300, True), (301, False)])
def test_length_threshold(length: int, fires: bool) -> None:
    log = InMemoryEventLog()
    reply = "x" * length
    _lost_then_reply(log, reply)
    if fires:
        assert _cont(log)(reply) == (UNDELIVERED_MESSAGE_NUDGE, False)
    else:
        _assert_parks_on(_cont(log), reply)
        assert not _corrections(log)


def test_surrounding_whitespace_does_not_count_toward_the_length() -> None:
    log = InMemoryEventLog()
    reply = "  " + "x" * 300 + "\n\n"
    _lost_then_reply(log, reply)
    assert _cont(log)(reply) == (UNDELIVERED_MESSAGE_NUDGE, False)


def _normal(log: EventLog) -> None:
    _respond(log, THINK, TEXT, TOOL)


def _one_thinking(log: EventLog) -> None:
    _respond(log, THINK, TOOL)


def _text_beside_thinking(log: EventLog) -> None:
    _respond(log, THINK, THINK, TEXT, TOOL)


def _from_a_subagent(log: EventLog) -> None:
    _respond(log, THINK, THINK, TOOL, agent_id=SUBAGENT)


def _then_user_message(log: EventLog) -> None:
    _respond(log, THINK, THINK, TOOL)
    log.append_user_message(agent_id=AGENT, message=SAY, subtype="reply")


def _then_engine_message(log: EventLog) -> None:
    _respond(log, THINK, THINK, TOOL)
    log.append_engine_message(agent_id=AGENT, message=SAY, subtype="continuation")


def _openai_raw(log: EventLog) -> None:
    log.append_agent_message(
        agent_id=AGENT, provider="openai", model="m", usage={}, stop_reason=None,
        raw={"output": [{"type": "reasoning"}, {"type": "reasoning"},
                        {"type": "function_call"}]})


def _codex_raw(log: EventLog) -> None:
    log.append_agent_message(
        agent_id=AGENT, provider="codex", model="m", usage={}, stop_reason=None,
        raw={"content": [{"type": "text", "text": "visible"}]})


def _text_earlier_in_the_message_id(log: EventLog) -> None:
    _respond(log, THINK, THINK, TEXT, TOOL, message_id="m1")
    _respond(log, TOOL, message_id="m1")


@pytest.mark.parametrize("before_reply", [
    _normal, _one_thinking, _text_beside_thinking, _from_a_subagent, _then_user_message,
    _then_engine_message, _openai_raw, _codex_raw, _text_earlier_in_the_message_id,
])
def test_non_matching_responses_do_not_fire(before_reply: Callable[[EventLog], None]) -> None:
    log = InMemoryEventLog()
    before_reply(log)
    _respond(log, {"type": "text", "text": STUB}, message_id="m2")
    _assert_parks_on(_cont(log), STUB)
    assert not _corrections(log)


def test_no_event_log_does_not_fire() -> None:
    _assert_parks_on(_cont(None), STUB)


def test_non_interactive_step_without_a_schema_never_fires() -> None:
    log = InMemoryEventLog()
    _lost_then_reply(log)
    _assert_parks_on(_cont(log, interactive=False, has_schema=False), STUB)
    assert not _corrections(log)


def test_schema_bearing_non_interactive_step_gets_only_the_completion_nudge() -> None:
    log = InMemoryEventLog()
    fn = _cont(log, interactive=False)
    assert fn(_lost_then_reply(log)) == (COMPLETION_NUDGE, False)
    assert not _corrections(log)
    with pytest.raises(AwaitingUserInput) as exc:
        fn("WAITING")
    assert exc.value.prompt == STUB
    subtypes = [e["payload"]["subtype"] for e in log.read() if e["type"] == "engine_message"]
    assert subtypes == ["completion"]


def test_a_second_lost_shape_after_the_correction_parks_on_the_resend() -> None:
    log = InMemoryEventLog()
    fn = _cont(log)
    assert fn(_lost_then_reply(log))[0] == UNDELIVERED_MESSAGE_NUDGE
    log.append_engine_message(agent_id=AGENT, message=SAY, subtype="continuation")
    _lost_then_reply(log, RESEND)
    _assert_parks_on(fn, RESEND)
    assert len(_corrections(log)) == 1


@pytest.mark.anyio
async def test_attended_yield_carries_the_resend_and_a_later_stop_fires_again() -> None:
    log = InMemoryEventLog()

    async def solicit(_prompt: str) -> str:
        return "blue"

    fn = _cont(log, user_input_fn=solicit)
    assert fn(_lost_then_reply(log))[0] == UNDELIVERED_MESSAGE_NUDGE
    log.append_engine_message(agent_id=AGENT, message=SAY, subtype="continuation")
    _respond(log, {"type": "text", "text": RESEND})
    pending, human = fn(RESEND)
    assert human is True
    assert await pending == "blue"
    [gate] = [e for e in log.read() if e["type"] == "awaiting_user_input"]
    assert gate["payload"]["last_agent_message"] == RESEND
    log.append_user_message(agent_id=AGENT, message=SAY, subtype="reply")
    assert fn(_lost_then_reply(log))[0] == UNDELIVERED_MESSAGE_NUDGE
    assert len(_corrections(log)) == 2


def test_a_pending_interject_wins_over_the_check() -> None:
    log = InMemoryEventLog()
    coord = InterruptCoordinator()
    coord.enqueue_interject("also check the docs")
    fn = _cont(log, coordinator=coord)
    assert fn(_lost_then_reply(log)) == ("also check the docs", True)
    assert not _corrections(log)


def test_an_interject_after_the_check_fired_clears_the_guard() -> None:
    log = InMemoryEventLog()
    coord = InterruptCoordinator()
    fn = _cont(log, coordinator=coord)
    assert fn(_lost_then_reply(log))[0] == UNDELIVERED_MESSAGE_NUDGE
    coord.enqueue_interject("also check the docs")
    assert fn(RESEND) == ("also check the docs", True)
    log.append_user_message(agent_id=AGENT, message=SAY, subtype="interject")
    assert fn(_lost_then_reply(log))[0] == UNDELIVERED_MESSAGE_NUDGE
    assert len(_corrections(log)) == 2


@pytest.mark.anyio
async def test_a_steer_after_the_check_fired_clears_the_guard() -> None:
    log = InMemoryEventLog()
    fn = _cont(log)
    assert fn(_lost_then_reply(log))[0] == UNDELIVERED_MESSAGE_NUDGE
    log.append_engine_message(agent_id=AGENT, message=SAY, subtype="continuation")

    async def solicit(_prompt: str) -> str:
        return ""

    steer = build_inline_solicit(user_input_fn=solicit, event_log=log, agent_id=AGENT,
                                 steer_prompt=INTERRUPT_STEER_HINT, interrupt=True)
    await steer(RESEND)
    log.append_engine_message(agent_id=AGENT, message=SAY, subtype="continuation")
    assert fn(_lost_then_reply(log))[0] == UNDELIVERED_MESSAGE_NUDGE
    assert len(_corrections(log)) == 2


def test_an_interject_delivered_at_a_tool_boundary_clears_the_guard() -> None:
    log = InMemoryEventLog()
    fn = _cont(log)
    assert fn(_lost_then_reply(log))[0] == UNDELIVERED_MESSAGE_NUDGE
    log.append_engine_message(agent_id=AGENT, message=SAY, subtype="continuation")
    log.append_user_message(agent_id=AGENT, message=SAY, subtype="interject")
    assert fn(_lost_then_reply(log))[0] == UNDELIVERED_MESSAGE_NUDGE
    assert len(_corrections(log)) == 2


def test_a_reopened_log_keeps_the_guard_until_the_next_yield(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    log = JsonlEventLog(path)
    assert _cont(log)(_lost_then_reply(log))[0] == UNDELIVERED_MESSAGE_NUDGE
    log.append_engine_message(agent_id=AGENT, message=SAY, subtype="continuation")
    reopened = JsonlEventLog(path)
    _lost_then_reply(reopened, RESEND)
    _assert_parks_on(_cont(reopened), RESEND)
    assert len(_corrections(reopened)) == 1


@pytest.mark.parametrize("older_events", [10, 5000])
def test_the_check_never_reads_the_log_however_long_it_is(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, older_events: int) -> None:
    path = tmp_path / "events.jsonl"
    with path.open("w") as f:
        for i in range(older_events):
            f.write(f'{{"id": "{i}", "agent_id": "{SUBAGENT}", "type": "tool_call", '
                    '"payload": {}}\n')
    log = JsonlEventLog(path)
    reply = _lost_then_reply(log)
    fn = _cont(log)
    reads: list[str] = []
    real_open = Path.open

    def spying_open(p: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if p == path and "a" not in mode:
            reads.append(mode)
        return real_open(p, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spying_open)
    assert fn(reply) == (UNDELIVERED_MESSAGE_NUDGE, False)
    assert reads == []
