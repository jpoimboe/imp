"""Parametrized provider conformance suite (spec §2). Each scenario runs
against only the pairs whose seam produces it; each pair's normalized record is
pinned to a committed golden captured from the pre-refactor runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.completion import build_continuation_fn

from tests.conformance_adapters import (
    OBSERVE,
    PAIRS,
    PUSH,
    run_scenario,
)
from tests.conformance_record import check_golden, record
from tests.cc_fakes import assistant as cc_assistant
from tests.cc_fakes import result as cc_result
from tests.cc_fakes import text as cc_text
from tests.cc_fakes import tool_use as cc_tool_use
from tests.anthropic_fakes import response as a_response
from tests.anthropic_fakes import text as a_text
from tests.anthropic_fakes import tool_use as a_tool_use
from tests.openai_fakes import resp_function_call, resp_message, resp_response
from tests.codex_fakes import (
    agent_message as cx_msg,
)
from tests.codex_fakes import (
    mcp_call as cx_call,
)
from tests.codex_fakes import (
    thread_started as cx_start,
)
from tests.codex_fakes import (
    turn_completed as cx_done,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_OK = {"status": "Success", "note": "ok"}


def _happy_script(pair: str):
    if pair == "anthropic":
        return [a_response(a_text("calling complete_step"),
                           a_tool_use("c1", "complete_step", _OK))]
    if pair == "openai":
        return [resp_response(
            resp_message("calling complete_step"),
            resp_function_call("c1", "complete_step",
                               '{"status": "Success", "note": "ok"}'))]
    if pair == "claude_code":
        return [cc_assistant(cc_text("calling complete_step"),
                             cc_tool_use("c1", "complete_step", _OK)),
                cc_result()]
    return [[cx_start("thr-1"), cx_msg("calling complete_step"),
             cx_call(name="complete_step", args=_OK, result="ok", item_id="cs-1"),
             cx_done(input_tokens=1, output_tokens=1)]]


@pytest.mark.anyio
@pytest.mark.parametrize("pair", PAIRS)
async def test_happy_path(pair: str, tmp_path: Path) -> None:
    driven = await run_scenario(pair, tmp_path, _happy_script(pair))
    # Falsifiable invariants (hold now; guard the refactor):
    assert driven.control[-1]["type"] == "agent_result"
    assert driven.control[-1]["payload"] == _OK
    log_type_set = {e["type"] for e in driven.log.read()}
    assert {"step_context", "engine_message", "agent_message"} <= log_type_set
    assert sum(1 for e in driven.log.read() if e["type"] == "step_context") == 1
    check_golden(f"happy__{pair}", record(driven))


def _multiround_script(pair: str):
    if pair == "anthropic":
        return [a_response(a_tool_use("u1", "read_file", {"path": "/project/data.txt"})),
                a_response(a_tool_use("c1", "complete_step", _OK))]
    if pair == "openai":
        return [resp_response(resp_function_call(
                    "u1", "read_file", '{"path": "/project/data.txt"}')),
                resp_response(resp_function_call(
                    "c1", "complete_step", '{"status": "Success", "note": "ok"}'))]
    if pair == "claude_code":
        # Both tool calls are in one SDK session (one receive_response() call):
        # read_file observed first, then complete_step terminates the session.
        return [cc_assistant(cc_tool_use("u1", "read_file", {"path": "/project/data.txt"})),
                cc_assistant(cc_tool_use("c1", "complete_step", _OK)),
                cc_result()]
    # codex: both MCP calls in one run_streamed() turn; no continuation_fn needed.
    return [[cx_start("thr-1"),
             cx_call(name="read_file", args={"path": "/project/data.txt"},
                     result="payload", item_id="u1"),
             cx_call(name="complete_step", args=_OK, result="ok", item_id="cs-1"),
             cx_done()]]


@pytest.mark.anyio
@pytest.mark.parametrize("pair", PAIRS)
async def test_multi_round_tools(pair: str, tmp_path: Path) -> None:
    driven = await run_scenario(
        pair, tmp_path, _multiround_script(pair),
        tools=frozenset({"complete_step", "read_file"}))
    assert driven.control[-1]["type"] == "agent_result"
    assert driven.control[-1]["payload"] == _OK
    log_types = [e["type"] for e in driven.log.read()]
    assert log_types.count("tool_call") >= 2  # read_file round + complete_step
    check_golden(f"multiround__{pair}", record(driven))


def _nudge_script(pair: str):
    if pair == "anthropic":
        return [a_response(a_text("here is my answer in prose"), stop_reason="end_turn"),
                a_response(a_tool_use("c1", "complete_step", _OK))]
    if pair == "openai":
        return [resp_response(resp_message("prose, no tool call")),
                resp_response(resp_function_call(
                    "c1", "complete_step", '{"status": "Success", "note": "ok"}'))]
    if pair == "claude_code":
        return [cc_assistant(cc_text("prose, no tool")), cc_result(),
                cc_assistant(cc_tool_use("c1", "complete_step", _OK)), cc_result()]
    return [[cx_start("thr-1"), cx_msg("prose, no tool"), cx_done()],
            [cx_call(name="complete_step", args=_OK, result="ok", item_id="cs-1"),
             cx_done()]]


@pytest.mark.anyio
@pytest.mark.parametrize("pair", PAIRS)
async def test_nudge_then_complete(pair: str, tmp_path: Path) -> None:
    driven = await run_scenario(
        pair, tmp_path, _nudge_script(pair),
        continuation_fn=lambda _last: ("please call complete_step", False))
    # Reaching agent_result proves the continuation drove a recovery turn.
    assert driven.control[-1]["type"] == "agent_result"
    assert driven.control[-1]["payload"] == _OK
    assert "error" not in [e["type"] for e in driven.control]
    check_golden(f"nudge__{pair}", record(driven))


def _invalid_script(pair: str):
    if pair == "anthropic":
        return [a_response(a_tool_use("c1", "complete_step", {"note": "no status"})),
                a_response(a_tool_use("c2", "complete_step", _OK))]
    if pair == "openai":
        return [resp_response(resp_function_call(
                    "c1", "complete_step", '{"note": "no status"}')),
                resp_response(resp_function_call(
                    "c2", "complete_step", '{"status": "Success", "note": "ok"}'))]
    if pair == "claude_code":
        return [cc_assistant(cc_tool_use("c1", "complete_step", {"note": "no status"})),
                cc_result(),
                cc_assistant(cc_tool_use("c2", "complete_step", _OK)), cc_result()]
    return [[cx_start("thr-1"),
             cx_call(name="complete_step", args={"note": "no status"},
                     result="step complete"),
             cx_done()],
            [cx_start("thr-1"),
             cx_call(name="complete_step", args=_OK, result="step complete"),
             cx_done()]]


@pytest.mark.anyio
@pytest.mark.parametrize("pair", PAIRS)
async def test_invalid_then_recovers(pair: str, tmp_path: Path) -> None:
    driven = await run_scenario(pair, tmp_path, _invalid_script(pair))
    assert driven.control[-1]["type"] == "agent_result"
    assert driven.control[-1]["payload"] == _OK
    log_types = [e["type"] for e in driven.log.read()]
    assert log_types.count("output_validation_nudge") == 1
    assert "error" not in [e["type"] for e in driven.control]
    check_golden(f"invalid__{pair}", record(driven))


def _park_script(pair: str):
    if pair == "anthropic":
        return [a_response(a_text("prose"), stop_reason="end_turn"),
                a_response(a_text("prose"), stop_reason="end_turn")]
    if pair == "openai":
        return [resp_response(resp_message("prose")),
                resp_response(resp_message("prose"))]
    if pair == "claude_code":
        return [cc_assistant(cc_text("prose")), cc_result(),
                cc_assistant(cc_text("prose")), cc_result()]
    return [[cx_start("thr-1"), cx_msg("prose"), cx_done()],
            [cx_msg("prose"), cx_done()]]


@pytest.mark.anyio
@pytest.mark.parametrize("pair", PAIRS)
async def test_park_on_nudge_exhaustion(pair: str, tmp_path: Path) -> None:
    from imp.engine.awaiting_user_input import AwaitingUserInput
    from imp.engine.state import InMemoryEventLog

    log = InMemoryEventLog()
    # user_input_fn=None + has_schema=True: first bare turn gets the one-shot
    # nudge, the second bare turn parks (raises AwaitingUserInput) — no human.
    cont = build_continuation_fn(
        user_input_fn=None, event_log=log, agent_id="step:p/s", has_schema=True)
    driven = await run_scenario(
        pair, tmp_path, _park_script(pair), event_log=log, continuation_fn=cont)
    assert isinstance(driven.raised, AwaitingUserInput)
    assert "agent_result" not in [e["type"] for e in driven.control]
    check_golden(f"park__{pair}", record(driven))
