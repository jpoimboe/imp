# tests/test_claude_code_inflight.py
from __future__ import annotations

import asyncio
import json

import pytest

from imp.agents.runner import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from imp.agents.transcript import captured_result_key
from imp.engine.state import InMemoryEventLog
from imp.providers.claude import (
    INFLIGHT_RESULT_BOUND_S,
    _correct_withheld_results,
    _InFlightOutcome,
    _restore_withheld_results,
    _RunState,
    _settle_inflight,
    _tool_use_block,
    correction_increment,
)

# Stands in for whatever the SDK sent; the point of the restore path is that it
# goes back verbatim, whatever it was.
FILLER = "the SDK's rejection filler"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_settle_reports_the_real_result_of_a_late_finishing_call() -> None:
    """The point of the bound: a call still running when the interrupt landed is
    waited out and reports what it actually did, not what the SDK guessed. The wait
    is the full production bound, so a loaded machine simply waits longer."""
    state = _RunState()
    state.register_inflight("k1", "write_file")

    async def _finish_late() -> None:
        await asyncio.sleep(0.05)
        state.resolve_inflight("k1", "wrote 12 bytes to notes.md", is_error=False)

    task = asyncio.create_task(_finish_late())
    settled = await _settle_inflight(state, INFLIGHT_RESULT_BOUND_S)
    await task

    assert [(s.key, s.name, s.content, s.is_error) for s in settled] == [
        ("k1", "write_file", "wrote 12 bytes to notes.md", False)]
    assert state.inflight == {}


@pytest.mark.anyio
async def test_settle_substitutes_the_uncertainty_body_when_the_bound_expires(
) -> None:
    """A wedged call yields an honest statement of uncertainty, per tool shape —
    never a claim of rejection, and never a fabricated returncode."""
    state = _RunState()
    state.register_inflight("k1", "write_file")
    state.register_inflight("k2", "bash")

    settled = {s.key: s for s in await _settle_inflight(state, 0.05)}

    assert settled["k1"].content == (
        "imp interrupted this turn; this call was still running and its outcome "
        "was not recorded. Verify the current state before relying on it.")
    assert settled["k1"].is_error is True
    assert json.loads(settled["k2"].content) == {
        "stdout": "",
        "stderr": settled["k1"].content,
        "returncode": None,
    }
    assert settled["k2"].is_error is True


@pytest.mark.anyio
async def test_settle_returns_immediately_when_nothing_is_in_flight() -> None:
    """The common case pays nothing — stalling an ordinary interrupt for the bound
    would be the regression this guards."""
    state = _RunState()
    assert await asyncio.wait_for(
        _settle_inflight(state, INFLIGHT_RESULT_BOUND_S), timeout=10) == []


def test_no_increment_when_nothing_was_in_flight() -> None:
    """The common interrupt must grow no preamble — an empty settle list yields
    nothing to prepend, which is what keeps the bare handoff instruction bare."""
    assert correction_increment([]) == ""


def test_increment_names_each_call_and_its_real_outcome() -> None:
    """The increment has to out-argue the SDK's "the tool use was rejected" in the
    model's own context: it labels that filler as filler and then states, per call,
    what imp's own execution actually produced."""
    inc = correction_increment([
        _InFlightOutcome(key="k1", name="write_file",
                         content="wrote 1 bytes to scratch", is_error=False),
        _InFlightOutcome(key="k2", name="bash",
                         content='{"stdout": "ok", "returncode": 0}',
                         is_error=False),
    ])

    assert inc.startswith(
        "[imp] This turn was interrupted while the calls below were still "
        "running.")
    assert "NOT a rejection, denial, or failure, and NOT anything the user did" in inc
    assert "do not re-run the calls on their account" in inc
    assert "- write_file: wrote 1 bytes to scratch" in inc
    assert '- bash: {"stdout": "ok", "returncode": 0}' in inc
    assert "account.\n\n- write_file:" in inc
    # Prepended verbatim to the next query, so it must end separated from it.
    assert inc.endswith("\n\n")


@pytest.mark.anyio
async def test_a_withheld_filler_is_replaced_by_imps_own_result() -> None:
    """imp is the executor of its own tools, so the log must carry what the tool
    really did — paired with the tool_call the crossing turn never journaled."""
    state = _RunState()
    args = {"path": "scratch", "content": "x"}
    key = captured_result_key("write_file", args)
    state.register_inflight(key, "write_file")

    assert state.withhold_inflight(
        ToolUseBlock(id="u1", name="mcp__imp__write_file", input=args),
        FILLER, journaled=False) is True

    log = InMemoryEventLog()
    _correct_withheld_results(log, "step:p/s", state,
                              {key: ("wrote 1 bytes to scratch", False)})

    events = list(log.read())
    assert [e["type"] for e in events] == ["tool_call", "tool_result"]
    assert events[0]["payload"] == {
        "id": "u1", "name": "write_file", "input": args}
    assert events[1]["payload"] == {
        "id": "u1", "content": "wrote 1 bytes to scratch", "error": False}
    assert not state.withheld


@pytest.mark.anyio
async def test_an_already_journaled_call_gains_only_its_result() -> None:
    """One call and one result per tool_use_id: when the streaming site already
    emitted the tool_call, the late append must not emit a second one."""
    state = _RunState()
    key = captured_result_key("write_file", {"path": "a"})
    state.register_inflight(key, "write_file")
    state.withhold_inflight(
        ToolUseBlock(id="u7", name="write_file", input={"path": "a"}),
        FILLER, journaled=True)

    log = InMemoryEventLog()
    _correct_withheld_results(log, "step:p/s", state, {key: ("wrote it", False)})

    assert [e["type"] for e in log.read()] == ["tool_result"]


@pytest.mark.anyio
async def test_a_call_imp_is_not_running_is_never_withheld() -> None:
    """Suppression is narrow: a filler imp has no better account of — a different
    call's args, or a tool_use imp never saw — stays in the log as today."""
    state = _RunState()
    state.register_inflight(
        captured_result_key("write_file", {"path": "a"}), "write_file")

    assert state.withhold_inflight(
        ToolUseBlock(id="u8", name="write_file", input={"path": "b"}),
        FILLER, journaled=False) is False
    assert state.withhold_inflight(None, FILLER, journaled=False) is False
    assert not state.withheld


@pytest.mark.anyio
async def test_a_withheld_result_imp_has_no_record_of_states_uncertainty() -> None:
    """The call resolved through an exception path, which writes no capture, in the
    window between the filler and the settle — so the merged map holds nothing for
    it. The log still gets its one result: honest uncertainty, never the filler."""
    state = _RunState()
    key = captured_result_key("bash", {"command": "sleep 5"})
    state.register_inflight(key, "bash")
    state.withhold_inflight(
        ToolUseBlock(id="u2", name="mcp__imp__bash", input={"command": "sleep 5"}),
        FILLER, journaled=True)

    log = InMemoryEventLog()
    _correct_withheld_results(log, "step:p/s", state, {})

    [ev] = [e for e in log.read() if e["type"] == "tool_result"]
    assert ev["payload"]["error"] is True
    assert json.loads(ev["payload"]["content"]) == {
        "stdout": "",
        "stderr": ("imp interrupted this turn; this call was still running and its "
                   "outcome was not recorded. Verify the current state before "
                   "relying on it."),
        "returncode": None}


@pytest.mark.anyio
async def test_a_turn_that_never_corrects_puts_the_filler_back() -> None:
    """The withhold is a deferral, not a deletion. A terminal outcome that runs no
    bounded await has nothing truer to say, so the SDK's account goes back verbatim
    and the call keeps the one record it has today."""
    state = _RunState()
    key = captured_result_key("write_file", {"path": "a"})
    state.register_inflight(key, "write_file")
    state.withhold_inflight(
        ToolUseBlock(id="u9", name="mcp__imp__write_file", input={"path": "a"}),
        FILLER, journaled=False)

    log = InMemoryEventLog()
    _restore_withheld_results(log, "step:p/s", state)

    events = list(log.read())
    assert [e["type"] for e in events] == ["tool_call", "tool_result"]
    assert events[1]["payload"] == {"id": "u9", "content": FILLER, "error": True}
    assert not state.withheld


def test_tool_use_block_finds_the_call_a_result_answers() -> None:
    """_pending_tools is empty on a crossing turn, so the in-memory transcript is the
    only source of the call's name and input."""
    messages = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(
            TextBlock(text="working"),
            ToolUseBlock(id="u1", name="mcp__imp__write_file",
                         input={"path": "a"}))),
        Message(role="user", content=(
            ToolResultBlock("u1", "rejected", is_error=True),)),
    ]

    found = _tool_use_block(messages, "u1")
    assert found is not None
    assert (found.name, found.input) == ("mcp__imp__write_file", {"path": "a"})
    assert _tool_use_block(messages, "u404") is None
