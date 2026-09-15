"""Classified terminal-error turns are rolled back so retries re-query."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imp.agents.classify import ResumeState, classify_resume
from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
from imp.agents.transcript import Transcript
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.paths import project_state_dir
from imp.providers.claude import ClaudeAgentRunner, _roll_back_error_turn
from imp.providers.claude_session_store import (
    resume_session_id,
    session_key,
    session_store_for,
)
from tests.cc_fakes import FakeClaudeSDKClient, assistant, chrome, result, text, tool_use


def test_roll_back_removes_trailing_assistant_turn(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    seed = Message(role="user", content=(TextBlock(text="go"),))
    err = Message(
        role="assistant",
        content=(TextBlock(text="Failed to authenticate. API Error: 401"),),
    )
    messages = [seed, err]
    t.append(seed)
    t.append(err)

    _roll_back_error_turn(messages, t)

    # In-memory list ends on the prior user turn.
    assert [m.role for m in messages] == ["user"]
    block = messages[0].content[0]
    assert isinstance(block, TextBlock)
    assert block.text == "go"

    # Persisted transcript is truncated to match.
    loaded = t.load()
    assert [m.role for m in loaded] == ["user"]
    loaded_block = loaded[0].content[0]
    assert isinstance(loaded_block, TextBlock)
    assert loaded_block.text == "go"


def test_roll_back_removes_only_most_recent_assistant_turn(tmp_path: Path) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    user1 = Message(role="user", content=(TextBlock(text="go"),))
    a1 = Message(role="assistant", content=(TextBlock(text="first assistant turn"),))
    user2 = Message(role="user", content=(TextBlock(text="continue"),))
    a2 = Message(role="assistant", content=(TextBlock(text="second assistant turn - error"),))
    messages = [user1, a1, user2, a2]
    for m in messages:
        t.append(m)

    _roll_back_error_turn(messages, t)

    # Only the last assistant turn (A2) is removed; A1 is intact.
    assert [m.role for m in messages] == ["user", "assistant", "user"]
    a1_block = messages[1].content[0]
    assert isinstance(a1_block, TextBlock)
    assert a1_block.text == "first assistant turn"

    # Persisted transcript matches in-memory list.
    loaded = t.load()
    assert [m.role for m in loaded] == ["user", "assistant", "user"]
    loaded_a1_block = loaded[1].content[0]
    assert isinstance(loaded_a1_block, TextBlock)
    assert loaded_a1_block.text == "first assistant turn"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t", description="", tools=frozenset({"complete_step"}), subagents=(),
        output_schema=OutputSchema(
            fields=(
                OutputField(name="status", type="enum",
                            values=("Success", "Error"), required=True),
                OutputField(name="note", type="string", required=False),
            ),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


def _inv(tmp_path: Path, log: InMemoryEventLog) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(),
        provider_name="claude_code", model_name="claude-sonnet-4-5",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow",
        ),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=log,
    )


@pytest.mark.anyio
async def test_termination_401_rolls_back_and_keeps_llm_event(tmp_path: Path) -> None:
    log = InMemoryEventLog()
    inv = _inv(tmp_path, log)

    scripted = [
        assistant(text(
            "Failed to authenticate. API Error: 401 Invalid authentication credentials")),
        result(is_error=True, api_error_status=401),
    ]

    def factory(_opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(_opts, scripted_messages=scripted)

    runner = ClaudeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    events = [ev async for ev in runner.run(inv)]

    errs = [e for e in events if e["type"] == "error"]
    assert len(errs) == 1, events
    assert errs[0]["payload"]["category"] == "auth"

    # AC1: transcript rolled back to the prior user turn -> SEND_TO_LLM on resume.
    loaded = Transcript(inv.transcript_dir / "transcript.jsonl").load()
    assert [m.role for m in loaded] == ["user"]
    assert classify_resume(loaded) is ResumeState.SEND_TO_LLM

    # AC4: the error turn's llm_response event remains queryable.
    assert any(e["type"] == "agent_message" for e in log.read())


@pytest.mark.anyio
async def test_classified_text_branch_rolls_back(tmp_path: Path) -> None:
    log = InMemoryEventLog()
    inv = _inv(tmp_path, log)

    # No is_error / api_error_status -> _termination_from_result returns None,
    # so the auth-prose turn is caught by the _classify_agent_text branch.
    scripted = [
        chrome("Invalid authentication credentials"),
        result(),
    ]

    def factory(_opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(_opts, scripted_messages=scripted)

    runner = ClaudeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    events = [ev async for ev in runner.run(inv)]

    errs = [e for e in events if e["type"] == "error"]
    assert len(errs) == 1, events
    assert errs[0]["payload"]["category"] == "auth_terminal"
    assert errs[0]["payload"]["detail"]["last_agent_text"] == (
        "Invalid authentication credentials")

    loaded = Transcript(inv.transcript_dir / "transcript.jsonl").load()
    assert [m.role for m in loaded] == ["user"]
    assert classify_resume(loaded) is ResumeState.SEND_TO_LLM


@pytest.mark.anyio
async def test_terminal_prose_error_event_carries_the_full_text(tmp_path: Path) -> None:
    log = InMemoryEventLog()
    inv = _inv(tmp_path, log)

    scripted = [chrome("Not logged in · Please run /login"), result()]

    def factory(_opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(_opts, scripted_messages=scripted)

    runner = ClaudeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    events = [ev async for ev in runner.run(inv)]

    errs = [e for e in events if e["type"] == "error"]
    assert len(errs) == 1, events
    assert errs[0]["payload"]["category"] == "auth_terminal"
    assert errs[0]["payload"]["message"] == (
        "claude CLI: Not logged in · Please run /login")


@pytest.mark.anyio
async def test_quota_pause_still_errors_and_rollback_is_harmless(
    tmp_path: Path,
) -> None:
    log = InMemoryEventLog()
    inv = _inv(tmp_path, log)

    scripted = [
        chrome("Credit balance is too low"),
        result(),
    ]

    def factory(_opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(_opts, scripted_messages=scripted)

    runner = ClaudeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    events = [ev async for ev in runner.run(inv)]

    errs = [e for e in events if e["type"] == "error"]
    assert len(errs) == 1, events
    assert errs[0]["payload"]["category"] == "quota"

    # The unconditional rollback fired for the Pause category too — harmless.
    loaded = Transcript(inv.transcript_dir / "transcript.jsonl").load()
    assert [m.role for m in loaded] == ["user"]


@pytest.mark.anyio
async def test_auth_fails_then_recovers_on_redrive(tmp_path: Path) -> None:
    log = InMemoryEventLog()
    inv = _inv(tmp_path, log)

    drive1 = [
        assistant(text("Failed to authenticate. API Error: 401")),
        result(is_error=True, api_error_status=401),
    ]
    runner1 = ClaudeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=lambda o: FakeClaudeSDKClient(o, scripted_messages=drive1))
    ev1 = [ev async for ev in runner1.run(inv)]
    assert any(e["type"] == "error" and e["payload"]["category"] == "auth"
               for e in ev1), ev1

    # Rolled back -> ready to genuinely re-query on the next drive.
    loaded = Transcript(inv.transcript_dir / "transcript.jsonl").load()
    assert classify_resume(loaded) is ResumeState.SEND_TO_LLM

    drive2 = [
        assistant(text("done"),
                  tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    runner2 = ClaudeAgentRunner(  # no user_input_fn: recovery needs none
        model_name="claude-sonnet-4-5",
        client_factory=lambda o: FakeClaudeSDKClient(o, scripted_messages=drive2))
    ev2 = [ev async for ev in runner2.run(inv)]

    assert ev2[-1]["type"] == "agent_result", ev2
    assert ev2[-1]["payload"]["status"] == "Success"

    # The bug's signature must be absent across the whole recovery.
    log_types = [e["type"] for e in log.read()]
    assert "awaiting_user_input" not in log_types


_TRAILING_AUTH_PROSE = (
    "Failed to authenticate. API Error: 401 OAuth access token has been revoked")


def _seed_three_turn_transcript(inv: AgentInvocation, design_text: str) -> Transcript:
    """Persist the mada48 shape: [user, assistant DESIGN, user reply]."""
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    for m in (
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(TextBlock(text=design_text),)),
        Message(role="user", content=(TextBlock(text="looks good, proceed"),)),
    ):
        t.append(m)
    return t


def test_roll_back_keeps_history_when_the_trailing_turn_is_a_user_turn(
    tmp_path: Path,
) -> None:
    t = Transcript(tmp_path / "transcript.jsonl")
    messages = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(role="assistant", content=(TextBlock(text="DESIGN: here is the plan"),)),
        Message(role="user", content=(TextBlock(text="looks good, proceed"),)),
    ]
    for m in messages:
        t.append(m)

    _roll_back_error_turn(messages, t)

    # No synthetic error turn exists, so nothing is deleted — in memory...
    assert [m.role for m in messages] == ["user", "assistant", "user"]
    design = messages[1].content[0]
    assert isinstance(design, TextBlock)
    assert design.text == "DESIGN: here is the plan"
    # ...and on disk.
    loaded = t.load()
    assert [m.role for m in loaded] == ["user", "assistant", "user"]
    loaded_design = loaded[1].content[0]
    assert isinstance(loaded_design, TextBlock)
    assert loaded_design.text == "DESIGN: here is the plan"


@pytest.mark.anyio
async def test_bare_error_turn_keeps_history_and_labels_terminated(
    tmp_path: Path,
) -> None:
    """A failing turn with no trailing assistant message: nothing is deleted, and
    the category comes from THIS turn's (absent) prose, not the stale auth turn."""
    log = InMemoryEventLog()
    inv = _inv(tmp_path, log)
    t = _seed_three_turn_transcript(inv, f"DESIGN: {_TRAILING_AUTH_PROSE}")

    scripted = [result(is_error=True)]

    runner = ClaudeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=lambda o: FakeClaudeSDKClient(o, scripted_messages=scripted))
    events = [ev async for ev in runner.run(inv)]

    errs = [e for e in events if e["type"] == "error"]
    assert len(errs) == 1, events
    assert errs[0]["payload"]["category"] == "terminated"
    assert errs[0]["payload"]["detail"]["last_agent_text"] == ""

    loaded = t.load()
    assert [m.role for m in loaded] == ["user", "assistant", "user"]
    design = loaded[1].content[0]
    assert isinstance(design, TextBlock)
    assert design.text == f"DESIGN: {_TRAILING_AUTH_PROSE}"


@pytest.mark.anyio
async def test_bare_error_turn_keeps_the_retry_warm(tmp_path: Path) -> None:
    """The surviving assistant turn keeps resume_base > 0, so the retry is a native
    resume and the session store is never emptied into an unlink."""
    log = InMemoryEventLog()
    inv = _inv(tmp_path, log)
    _seed_three_turn_transcript(inv, "DESIGN: here is the plan")
    store = session_store_for(inv.transcript_dir)
    skey = session_key(inv.quest_context.project_root, "step:p/s")

    failing = ClaudeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=lambda o: FakeClaudeSDKClient(
            o, scripted_messages=[result(is_error=True)]))
    [ev async for ev in failing.run(inv)]

    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=[
            assistant(text("ok"),
                      tool_use("c1", "complete_step", {"status": "Success"})),
            result(),
        ])
        created.append(c)
        return c

    retry = ClaudeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory)
    ev2 = [ev async for ev in retry.run(inv)]

    opts = created[0].opts
    # Native warm resume, NOT the fresh-stream respawn that resume_base == 0 picks.
    assert opts.resume == resume_session_id("step:p/s")
    assert opts.session_id is None

    # The store survives with the delivered assistant turn in it.
    body = await store.load(skey)
    assert body is not None
    assert [r["message"]["role"] for r in body] == ["user", "assistant"]

    # Only the unfolded tail streams as the delta — history is not replayed.
    streamed = created[0].queries[0]
    assert len(streamed) == 1
    assert streamed[0]["message"]["content"] == "looks good, proceed"

    assert ev2[-1]["type"] == "agent_result", ev2
    assert ev2[-1]["payload"]["status"] == "Success"


@pytest.mark.anyio
async def test_repeated_trailing_failures_remove_only_their_own_error_turn(
    tmp_path: Path,
) -> None:
    """N consecutive failures WITH trailing prose still delete exactly the turn each
    produced: the delivered design turn survives every lap and keeps reaching the
    model as role=assistant."""
    log = InMemoryEventLog()
    inv = _inv(tmp_path, log)
    t = _seed_three_turn_transcript(inv, "DESIGN: here is the plan")
    store = session_store_for(inv.transcript_dir)
    skey = session_key(inv.quest_context.project_root, "step:p/s")

    for lap in range(3):
        runner = ClaudeAgentRunner(
            model_name="claude-sonnet-4-5",
            client_factory=lambda o: FakeClaudeSDKClient(o, scripted_messages=[
                assistant(text(_TRAILING_AUTH_PROSE)),
                result(is_error=True),
            ]))
        events = [ev async for ev in runner.run(inv)]

        errs = [e for e in events if e["type"] == "error"]
        assert len(errs) == 1, (lap, events)
        # Anchored trailing prose still labels the turn off that prose; the
        # routable 401 it carries makes that label retryable auth.
        assert errs[0]["payload"]["category"] == "auth", lap
        assert errs[0]["payload"]["detail"]["last_agent_text"] == (
            _TRAILING_AUTH_PROSE), lap

        loaded = t.load()
        assert [m.role for m in loaded] == ["user", "assistant", "user"], lap
        design = loaded[1].content[0]
        assert isinstance(design, TextBlock)
        assert design.text == "DESIGN: here is the plan", lap

        body = await store.load(skey)
        assert body is not None, lap
        assert [r["message"]["role"] for r in body] == ["user", "assistant"], lap
