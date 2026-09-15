"""Parametrized provider conformance suite (spec §2). Each scenario runs
against only the pairs whose seam produces it; each pair's normalized record is
pinned to a committed golden captured from the pre-refactor runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.loop_guard import DEFAULT_REPEAT_K
from imp.engine.completion import build_continuation_fn
from tests.anthropic_fakes import response as a_response
from tests.anthropic_fakes import text as a_text
from tests.anthropic_fakes import tool_use as a_tool_use
from tests.cc_fakes import assistant as cc_assistant
from tests.cc_fakes import chrome as cc_chrome
from tests.cc_fakes import result as cc_result
from tests.cc_fakes import text as cc_text
from tests.cc_fakes import tool_result as cc_tool_result
from tests.cc_fakes import tool_use as cc_tool_use
from tests.cc_fakes import user as cc_user
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
from tests.conformance_adapters import (
    OBSERVE,
    PAIRS,
    PUSH,
    build,
    drive,
    run_scenario,
)
from tests.conformance_record import check_golden, record
from tests.openai_fakes import resp_function_call, resp_message, resp_response


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
                cc_user(cc_tool_result("u1", "payload")),
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


def _ceiling_script(pair: str):
    if pair == "anthropic":
        return [a_response(a_tool_use("u1", "read_file", {"path": "/project/x"})),
                a_response(a_tool_use("u2", "read_file", {"path": "/project/y"})),
                a_response(a_tool_use("u3", "read_file", {"path": "/project/z"}))]
    return [resp_response(resp_function_call("c1", "read_file", '{"path": "/project/x"}'),
                          resp_function_call("c2", "read_file", '{"path": "/project/y"}')),
            resp_response(resp_function_call("c3", "read_file", '{"path": "/project/z"}'),
                          resp_function_call("c4", "read_file", '{"path": "/project/w"}'))]


@pytest.mark.anyio
@pytest.mark.parametrize("pair", PUSH)
async def test_ceiling_trip(pair: str, tmp_path: Path) -> None:
    driven = await run_scenario(
        pair, tmp_path, _ceiling_script(pair),
        tools=frozenset({"complete_step", "read_file"}), max_tool_calls=2)
    assert driven.control[-1]["type"] == "error"
    assert driven.control[-1]["payload"]["category"] == "config"
    assert driven.control[-1]["payload"]["kind"] == "tool_call_ceiling_exceeded"
    check_golden(f"ceiling__{pair}", record(driven))


def _classify_script(pair: str):
    if pair == "claude_code":
        return [cc_chrome("Invalid API key — got 401. Cannot continue."),
                cc_result()]
    return [[cx_start("thr-1"),
             cx_msg("Invalid API key — got 401. Cannot continue."),
             cx_done()]]


@pytest.mark.anyio
@pytest.mark.parametrize("pair", OBSERVE)
async def test_classify_before_nudge(pair: str, tmp_path: Path) -> None:
    consulted = {"hit": False}

    def cont(_last: str):
        consulted["hit"] = True
        return "nudge", False

    driven = await run_scenario(
        pair, tmp_path, _classify_script(pair), continuation_fn=cont)
    assert driven.control[-1]["type"] == "error"
    assert driven.control[-1]["payload"]["category"] == "auth_terminal"
    # The classified Error short-circuits BEFORE the pointless nudge.
    assert consulted["hit"] is False
    check_golden(f"classify__{pair}", record(driven))


@pytest.mark.anyio
async def test_tool_killed_event_codex(tmp_path: Path, monkeypatch) -> None:
    import imp.providers.codex_runner as cr

    orig = cr.process_event

    def _patched(*a, **k):
        rs = k.get("run_state")
        ev = a[0]
        if (rs is not None and getattr(ev, "type", None) == "item.completed"
                and getattr(getattr(ev, "item", None), "tool", None) == "bash"):
            rs.killed_tool_uses.append({"id": "kill-1", "name": "bash", "signal": 9})
        return orig(*a, **k)

    monkeypatch.setattr(cr, "process_event", _patched)

    scripted = [[cx_start("thr-1"),
                 cx_call(name="bash", args={"command": "sleep 1"}, result="", item_id="b1"),
                 cx_call(name="complete_step", args=_OK, result="ok", item_id="cs-1"),
                 cx_done()]]
    runner, inv = build("codex", tmp_path, scripted,
                        tools=frozenset({"complete_step", "bash"}))
    driven = await drive(runner, inv)

    log = list(driven.log.read())
    killed = [e for e in log if e["type"] == "tool_killed"]
    assert killed and killed[0]["payload"]["name"] == "bash"
    assert killed[0]["payload"]["signal"] == 9
    # tool_killed is an audit-log event, never a yielded control signal.
    assert "tool_killed" not in [e["type"] for e in driven.control]
    assert driven.control[-1]["type"] == "agent_result"
    check_golden("toolkill__codex", record(driven))


@pytest.mark.anyio
async def test_fatal_tool_error_codex(tmp_path: Path, monkeypatch) -> None:
    import imp.providers.codex_runner as cr

    orig = cr.process_event

    def _patched(*a, **k):
        rs = k.get("run_state")
        if rs is not None and getattr(a[0], "type", None) == "turn.completed":
            rs.fatal_tool_error = ("lint", "config missing")
        return orig(*a, **k)

    monkeypatch.setattr(cr, "process_event", _patched)

    scripted = [[cx_start("thr-1"), cx_msg("calling lint"), cx_done(output_tokens=1)],
                [cx_msg("should never run"), cx_done()]]
    runner, inv = build("codex", tmp_path, scripted)
    driven = await drive(runner, inv)

    assert driven.control[-1]["type"] == "error"
    assert driven.control[-1]["payload"]["category"] == "config"
    assert driven.control[-1]["payload"]["kind"] == "fatal_tool_error"
    check_golden("fatal__codex", record(driven))


@pytest.mark.anyio
async def test_dangling_tool_use_repair_claude_code(tmp_path: Path) -> None:
    from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript
    from imp.engine.state import InMemoryEventLog
    from imp.providers.claude_code import ClaudeCodeAgentRunner
    from imp.providers.claude_session_store import resume_session_id
    from tests.conformance_adapters import build, drive

    (tmp_path / "art").mkdir(exist_ok=True)
    log = InMemoryEventLog()
    # Build inv via the adapter but supply our own capturing factory below.
    _runner_unused, inv = build("claude_code", tmp_path, [], event_log=log,
                                agent_id="step:p/s")
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="t0", name="mcp__imp__read_file", input={"path": "a.txt"}),)))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="t0", content="ALPHA", is_error=False),)))
    # Mid-tool interrupt: a bare assistant tool_use with NO tool_result.
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="t1", name="mcp__imp__read_file", input={"path": "b.txt"}),)))

    from tests.cc_fakes import FakeClaudeSDKClient
    scripted = [cc_assistant(cc_tool_use("c1", "complete_step", _OK)), cc_result()]
    created: list[FakeClaudeSDKClient] = []

    def factory(opts):
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5", client_factory=factory)
    driven = await drive(runner, inv)

    # transcript_invalid abort did NOT fire; the run completed.
    assert driven.control[-1]["type"] == "agent_result"
    assert not any(e["type"] == "error"
                   and e["payload"].get("kind") == "transcript_invalid"
                   for e in driven.control)
    assert created[0].opts.resume == resume_session_id("step:p/s")
    # DANGLING native resume STREAMS a synth tool_result for t1.
    streamed = created[0].queries[0]
    assert streamed[0]["message"]["content"][0]["type"] == "tool_result"
    assert streamed[0]["message"]["content"][0]["tool_use_id"] == "t1"
    check_golden("dangling__claude_code", record(driven))


@pytest.mark.anyio
async def test_resume_emits_no_step_context_codex(tmp_path: Path) -> None:
    from imp.providers.codex_runner import CodexAgentRunner
    from tests.codex_fakes import FakeCodex, seed_codex_resume_state

    _runner_unused, inv = build("codex", tmp_path, [], balance_on_resume=True)
    thread_id = "thr-resume-1"
    rel = f"sessions/2026/06/05/rollout-2026-06-05T00-00-00-{thread_id}.jsonl"
    state = seed_codex_resume_state(
        tmp_path / "tx", thread_id=thread_id, restore_path=rel)
    object.__setattr__(inv, "native_resume_state", state)

    scripted = [[
        cx_start(thread_id),
        cx_msg("resumed; calling complete_step"),
        cx_call(name="complete_step", args=_OK, result="ok", item_id="cs-1"),
        cx_done(input_tokens=1, output_tokens=1),
    ]]
    fake = FakeCodex(scripted_turns=scripted)
    runner = CodexAgentRunner(
        model_name="gpt-5.5", client_factory=lambda options=None: fake)
    runner._canonical_auth_override = tmp_path / "auth.json"

    driven = await drive(runner, inv)

    assert sum(1 for e in driven.log.read() if e["type"] == "step_context") == 0
    check_golden("resume_step_context__codex", record(driven))


@pytest.mark.anyio
async def test_cross_session_terminal_type_and_payload_and_step_context(
    tmp_path: Path,
) -> None:
    """AC #2(a) identical terminal type; (c) step_context exactly once; plus the
    happy-path agent_result payload parity that test_three_runner_parity pinned."""
    terminals, payloads = set(), []
    for pair in PAIRS:
        root = tmp_path / pair
        root.mkdir()
        driven = await run_scenario(pair, root, _happy_script(pair))
        terminals.add(driven.control[-1]["type"])
        payloads.append(driven.control[-1]["payload"])
        assert sum(1 for e in driven.log.read()
                   if e["type"] == "step_context") == 1
    assert terminals == {"agent_result"}
    assert all(p == _OK for p in payloads)


def _repeated_invalid_script(pair: str):
    n = DEFAULT_REPEAT_K + 2
    if pair == "anthropic":
        return [a_response(a_tool_use(f"c{i}", "complete_step", {"note": "no status"}))
                for i in range(n)]
    if pair == "openai":
        return [resp_response(resp_function_call(
                    f"c{i}", "complete_step", '{"note": "no status"}')) for i in range(n)]
    if pair == "claude_code":
        out = []
        for i in range(n):
            out += [cc_assistant(cc_tool_use(f"c{i}", "complete_step", {"note": "no status"})),
                    cc_result()]
        return out
    return [[cx_start("thr-1"),
             cx_call(name="complete_step", args={"note": "no status"}, result="x"),
             cx_done()] for _ in range(n)]


@pytest.mark.anyio
@pytest.mark.parametrize("pair", PAIRS)
async def test_cross_session_error_kind_identical(pair: str, tmp_path: Path) -> None:
    """AC #2(b): the same (category, kind) error pair across all four Sessions on
    a shared error scenario (repeated malformed complete_step -> loop guard)."""
    driven = await run_scenario(pair, tmp_path, _repeated_invalid_script(pair))
    assert driven.control[-1]["type"] == "error"
    assert (driven.control[-1]["payload"]["category"],
            driven.control[-1]["payload"]["kind"]) == ("loop", "repeated_failure")
    check_golden(f"repeated_invalid__{pair}", record(driven))
