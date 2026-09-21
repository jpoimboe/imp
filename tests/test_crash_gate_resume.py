# tests/test_crash_gate_resume.py
"""A crash parked at an unanswered question re-solicits the human on resume.

tests/test_session_daemon.py covers the daemon arming resume_should_continue=False;
this module pins the other half of the chain. With that disposition the re-entered
step emits the gate, the answer the human typed — buffered by an interactive
resume, or carried by `imp resume --message` — is delivered as their reply, and the
nudge that told the agent to carry on is never emitted. With no answer at all, an
unattended resume parks at the gate instead of continuing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.agents.transcript import RESUME_CONTINUE_NUDGE
from imp.paths import project_state_dir
from tests.async_wait import HANG_BACKSTOP, wait_until


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_QUESTION = "Does part two look right?"
_ANSWER = "looks right, ship it"


def _agent_def() -> Any:
    from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc

    return AgentDef(
        name="t", description="",
        tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


class _Coord:
    """No queued interjects, no watchdog bookkeeping. `escalation_event` is what
    the headless solicit trips when it has no message left to deliver."""

    def __init__(self) -> None:
        self.escalation_event = anyio.Event()

    def drain_interject(self) -> str | None:
        return None

    def reset(self) -> None:
        pass

    def note_activity(self) -> None:
        pass

    def begin_tool_watchdog(self) -> None:
        pass

    def end_tool_watchdog(self) -> None:
        pass

    def begin_solicit_watchdog(self) -> None:
        pass

    def end_solicit_watchdog(self) -> None:
        pass


def _seed_parked_transcript(inv_dir: Path) -> None:
    """The crash-at-a-question shape: the step's transcript ends on the agent's own
    question with nothing after it -> classify_resume AWAITING_USER."""
    from imp.agents.runner import Message, TextBlock
    from imp.agents.transcript import Transcript

    transcript = Transcript(inv_dir / "transcript.jsonl")
    transcript.append(Message(role="user", content=(TextBlock(text="go"),)))
    transcript.append(
        Message(role="assistant", content=(TextBlock(text=_QUESTION),)))


async def _resume_at_the_gate(
    tmp_path: Path, solicit: Any, coord: Any,
) -> tuple[Any, list[Any]]:
    """Resume a claude step parked at a question with resume_should_continue=False
    — the disposition a crash at a gate now arms. Returns (event log, transcript)."""
    from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
    from imp.agents.transcript import Transcript
    from imp.engine.state import InMemoryEventLog
    from imp.providers.claude import ClaudeAgentRunner
    from tests.cc_fakes import FakeClaudeSDKClient, assistant, result
    from tests.cc_fakes import text as cc_text
    from tests.cc_fakes import tool_use as cc_tool_use

    inv_dir = tmp_path / "step"
    inv_dir.mkdir(parents=True, exist_ok=True)
    _seed_parked_transcript(inv_dir)

    scripted = [
        assistant(cc_text("thanks"),
                  cc_tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    log = InMemoryEventLog()
    runner = ClaudeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
        user_input_fn=solicit)
    inv = AgentInvocation(
        agent_def=_agent_def(),
        provider_name="claude",
        model_name="claude-sonnet-4-5",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=(project_state_dir(tmp_path) / "quests" / "test-quest"
                          / "workflow"),
        ),
        transcript_dir=inv_dir,
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=log,
        coordinator=coord,
        resume_should_continue=False,
    )
    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_result", events[-1]
    return log, Transcript(inv_dir / "transcript.jsonl").load()


@pytest.mark.anyio
@pytest.mark.parametrize("channel", ["buffered", "message"])
async def test_resume_at_an_unanswered_gate_delivers_the_human_answer(
    tmp_path: Path, channel: str,
) -> None:
    """The gate re-opens, the typed answer is journaled as the human's reply, and
    no continuation nudge is emitted for the re-entry. `buffered` is the
    interactive resume prompt; `message` is `imp resume --headless --message`."""
    from imp.cli.inert_resume import make_buffered_solicit
    from imp.cli.session_run import make_headless_solicit

    coord = _Coord()

    async def _tty(prompt: str, interrupt: bool = False) -> str:
        raise AssertionError("the typed answer must satisfy the first gate")

    solicit = (make_buffered_solicit(_ANSWER, _tty) if channel == "buffered"
               else make_headless_solicit(_ANSWER, coord))

    log, messages = await _resume_at_the_gate(tmp_path, solicit, coord)
    events = list(log.read())

    # The gate was re-emitted, carrying the question the agent had asked.
    gates = [e for e in events if e["type"] == "awaiting_user_input"]
    assert len(gates) == 1
    assert gates[0]["payload"]["last_agent_message"] == _QUESTION
    # The answer reached the agent as the human's own reply.
    replies = [e for e in events if e["type"] == "user_message"
               and e["payload"].get("subtype") == "reply"]
    assert len(replies) == 1
    assert [b["text"] for b in replies[0]["payload"]["content"]] == [_ANSWER]
    # ...and as the transcript's next user turn, so it drives the next LLM turn.
    assert messages[2].role == "user"
    assert messages[2].content[0].text == _ANSWER
    # The nudge that instructed the agent to carry on is gone for this re-entry.
    conts = [e for e in events if e["type"] == "engine_message"
             and e["payload"].get("subtype") == "continuation"]
    assert RESUME_CONTINUE_NUDGE not in [
        c["payload"]["content"][0]["text"] for c in conts]


@pytest.mark.anyio
async def test_headless_resume_with_no_message_parks_at_the_gate() -> None:
    """The deliberate behaviour change: an unattended resume after a crash at a
    genuine question parks — the gate is emitted and a clean detach is signalled —
    instead of the agent being told to continue. The solicit never returns, so no
    stray reply is delivered to the gate."""
    import imp.engine.agent_run as ar
    from imp.cli.session_run import make_headless_solicit
    from imp.engine.state import InMemoryEventLog

    coord = _Coord()
    log = InMemoryEventLog()
    resolved: dict[str, Any] = {}

    async def _wait() -> None:
        resolved["reply"] = await ar._resolve_wait_reply(
            coord, make_headless_solicit(None, coord), False,
            elog=log, agent_id="step:p/s", last_agent_message=_QUESTION)

    with anyio.fail_after(HANG_BACKSTOP):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            tg.start_soon(_wait)
            await wait_until(lambda: coord.escalation_event.is_set())
            gates = [e for e in log.read() if e["type"] == "awaiting_user_input"]
            assert len(gates) == 1
            assert gates[0]["payload"]["last_agent_message"] == _QUESTION
            assert "reply" not in resolved       # parked: nothing was answered
            tg.cancel_scope.cancel()
