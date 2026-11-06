"""EventLog.response_before_last: the agent's previous response this turn, block shape only."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.engine.state import EventLog, InMemoryEventLog, JsonlEventLog, ResponseShape

AGENT = "step:p/s"
SUBAGENT = "step:p/s/sub"
THINK = {"type": "thinking", "thinking": "hidden"}
TOOL = {"type": "tool_use", "id": "u1", "name": "read_file", "input": {}}
TEXT = {"type": "text", "text": "visible"}
LOST = ResponseShape(thinking_blocks=2, has_text=False)
SAY = Message(role="user", content=(TextBlock(text="hello"),))


def _respond(log: EventLog, *blocks: dict[str, Any], agent_id: str = AGENT,
             message_id: str | None = None) -> None:
    log.append_agent_message(agent_id=agent_id, provider="p", model="m",
                             raw={"content": list(blocks)}, usage={}, stop_reason=None,
                             message_id=message_id)


@pytest.fixture(params=["memory", "jsonl"])
def log(request: pytest.FixtureRequest, tmp_path: Path) -> EventLog:
    if request.param == "memory":
        return InMemoryEventLog()
    return JsonlEventLog(tmp_path / "events.jsonl")


def test_no_earlier_response_means_none(log: EventLog) -> None:
    assert log.response_before_last(AGENT) is None
    _respond(log, TEXT)
    assert log.response_before_last(AGENT) is None
    assert log.response_before_last(None) is None


def test_reports_the_response_before_the_latest(log: EventLog) -> None:
    _respond(log, THINK, THINK, TOOL)
    _respond(log, TEXT)
    assert log.response_before_last(AGENT) == LOST
    _respond(log, THINK, TEXT, TOOL)
    _respond(log, TEXT)
    assert log.response_before_last(AGENT) == ResponseShape(1, True)


def test_events_sharing_a_message_id_are_one_response(log: EventLog) -> None:
    _respond(log, THINK, THINK, TOOL, message_id="m1")
    _respond(log, TOOL, message_id="m1")
    _respond(log, TEXT, message_id="m2")
    assert log.response_before_last(AGENT) == LOST


def test_a_latest_response_spanning_two_events_keeps_the_one_before_it(log: EventLog) -> None:
    _respond(log, THINK, THINK, TOOL)
    _respond(log, TEXT, message_id="m2")
    _respond(log, TEXT, message_id="m2")
    assert log.response_before_last(AGENT) == LOST


def test_text_in_any_event_of_the_response_counts(log: EventLog) -> None:
    _respond(log, THINK, THINK, TEXT, TOOL, message_id="m1")
    _respond(log, TOOL, message_id="m1")
    _respond(log, TEXT, message_id="m2")
    assert log.response_before_last(AGENT) == ResponseShape(2, True)


def test_thinking_counts_across_the_events_of_a_response(log: EventLog) -> None:
    _respond(log, THINK, TOOL, message_id="m1")
    _respond(log, THINK, TOOL, message_id="m1")
    _respond(log, TEXT, message_id="m2")
    assert log.response_before_last(AGENT) == LOST


def test_events_without_a_message_id_are_separate_responses(log: EventLog) -> None:
    _respond(log, TEXT)
    _respond(log, THINK, THINK, TOOL)
    _respond(log, TEXT)
    assert log.response_before_last(AGENT) == LOST


def test_whitespace_text_is_not_text(log: EventLog) -> None:
    _respond(log, THINK, THINK, {"type": "text", "text": " \n\t"}, TOOL)
    _respond(log, TEXT)
    assert log.response_before_last(AGENT) == LOST


@pytest.mark.parametrize("append", ["user_message", "engine_message"])
def test_a_message_to_the_agent_starts_a_new_turn(log: EventLog, append: str) -> None:
    _respond(log, THINK, THINK, TOOL)
    getattr(log, f"append_{append}")(agent_id=AGENT, message=SAY, subtype="reply")
    _respond(log, TEXT)
    assert log.response_before_last(AGENT) is None


def test_tool_events_stay_inside_the_turn(log: EventLog) -> None:
    _respond(log, THINK, THINK, TOOL)
    log.append_tool_call(agent_id=AGENT,
                         block=ToolUseBlock(id="u1", name="read_file", input={}))
    log.append_tool_result(agent_id=AGENT,
                           block=ToolResultBlock(tool_use_id="u1", content="ok"))
    log.append(agent_id=AGENT, type="thinking", payload={"content": "hidden"})
    _respond(log, TEXT)
    assert log.response_before_last(AGENT) == LOST


def test_another_agents_events_change_nothing(log: EventLog) -> None:
    _respond(log, THINK, THINK, TOOL)
    _respond(log, THINK, THINK, TOOL, agent_id=SUBAGENT)
    log.append_user_message(agent_id=SUBAGENT, message=SAY, subtype="reply")
    log.append_engine_message(agent_id=SUBAGENT, message=SAY, subtype="continuation")
    _respond(log, TEXT)
    assert log.response_before_last(AGENT) == LOST


def test_each_agent_keeps_its_own_turn(log: EventLog) -> None:
    _respond(log, THINK, THINK, TOOL, agent_id=SUBAGENT)
    _respond(log, TEXT, agent_id=SUBAGENT)
    _respond(log, TEXT)
    assert log.response_before_last(SUBAGENT) == LOST
    assert log.response_before_last(AGENT) is None


@pytest.mark.parametrize("raw", [
    {"output": [{"type": "reasoning"}, {"type": "message"}]},
    {"content": None},
    {"content": "text"},
    {"content": ["text", 1]},
    {},
    "text",
    None,
])
def test_a_raw_without_content_blocks_has_an_empty_shape(log: EventLog, raw: Any) -> None:
    log.append_agent_message(agent_id=AGENT, provider="p", model="m", raw=raw,
                             usage={}, stop_reason=None)
    _respond(log, TEXT)
    assert log.response_before_last(AGENT) == ResponseShape(0, False)


def test_reopening_a_log_restores_the_turn(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    writer = JsonlEventLog(path)
    _respond(writer, THINK, THINK, TOOL, message_id="m1")
    _respond(writer, TOOL, message_id="m1")
    writer.update_last_agent_message_usage(agent_id=AGENT, usage={"input_tokens": 1})
    _respond(writer, TEXT, message_id="m2")
    with path.open("a") as f:
        f.write("not json\n")
        f.write(json.dumps({"agent_id": AGENT, "type": "agent_message", "payload": "odd"}) + "\n")
    assert JsonlEventLog(path).response_before_last(AGENT) == LOST


@pytest.mark.parametrize("append", ["user_message", "engine_message"])
def test_reopening_a_log_replays_the_turn_reset(tmp_path: Path, append: str) -> None:
    path = tmp_path / "events.jsonl"
    writer = JsonlEventLog(path)
    _respond(writer, THINK, THINK, TOOL)
    getattr(writer, f"append_{append}")(agent_id=AGENT, message=SAY, subtype="reply")
    _respond(writer, TEXT)
    assert JsonlEventLog(path).response_before_last(AGENT) is None


def test_reopening_a_log_keeps_agents_apart(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    writer = JsonlEventLog(path)
    _respond(writer, THINK, THINK, TOOL)
    writer.append_user_message(agent_id=SUBAGENT, message=SAY, subtype="reply")
    _respond(writer, THINK, TOOL, agent_id=SUBAGENT)
    _respond(writer, TEXT)
    reopened = JsonlEventLog(path)
    assert reopened.response_before_last(AGENT) == LOST
    assert reopened.response_before_last(SUBAGENT) is None


def _correct(log: EventLog, agent_id: str = AGENT) -> None:
    log.append_engine_message(agent_id=agent_id, message=SAY, subtype="undelivered_message")


def test_no_resend_is_outstanding_initially(log: EventLog) -> None:
    assert not log.resend_outstanding(AGENT)
    assert not log.resend_outstanding(None)


def test_a_correction_leaves_a_resend_outstanding_through_its_continuation(log: EventLog) -> None:
    _correct(log)
    assert log.resend_outstanding(AGENT)
    log.append_engine_message(agent_id=AGENT, message=SAY, subtype="continuation")
    assert log.resend_outstanding(AGENT)
    assert not log.resend_outstanding(None)


def _user(subtype: str) -> Callable[[EventLog], None]:
    return lambda log: log.append_user_message(agent_id=AGENT, message=SAY, subtype=subtype)


def _gate(log: EventLog) -> None:
    log.append(agent_id=AGENT, type="awaiting_user_input",
               payload={"steer_hint": "", "last_agent_message": "q", "interrupt": False})


def _agent_prompt(log: EventLog) -> None:
    log.append_engine_message(agent_id=AGENT, message=SAY, subtype="agent_prompt")


CLEARERS = [_user("reply"), _user("interject"), _gate, _agent_prompt]


@pytest.mark.parametrize("clear", CLEARERS)
def test_a_yield_or_a_message_to_the_agent_clears_the_resend(
        log: EventLog, clear: Callable[[EventLog], None]) -> None:
    _correct(log)
    clear(log)
    assert not log.resend_outstanding(AGENT)


def test_another_agents_events_neither_set_nor_clear_the_resend(log: EventLog) -> None:
    _correct(log, SUBAGENT)
    assert not log.resend_outstanding(AGENT)
    _correct(log)
    log.append_user_message(agent_id=SUBAGENT, message=SAY, subtype="reply")
    assert log.resend_outstanding(AGENT)
    assert not log.resend_outstanding(SUBAGENT)


@pytest.mark.parametrize("clear", [None, *CLEARERS])
def test_reopening_a_log_restores_the_resend_state(
        tmp_path: Path, clear: Callable[[EventLog], None] | None) -> None:
    path = tmp_path / "events.jsonl"
    writer = JsonlEventLog(path)
    _correct(writer)
    if clear is not None:
        clear(writer)
    assert JsonlEventLog(path).resend_outstanding(AGENT) is (clear is None)
