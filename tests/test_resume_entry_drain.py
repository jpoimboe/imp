# tests/test_resume_entry_drain.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.agent_run import drain_all_interjects_at_resume


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_drain_all_appends_in_order_and_clears() -> None:
    class _Coord:
        def __init__(self) -> None:
            self._q = ["one", "two"]
        def drain_interject(self):
            return self._q.pop(0) if self._q else None
    coord = _Coord()
    # The provider `messages` list (raw Messages appended) is SEPARATE from the
    # fake transcript/elog sink list — otherwise bare Messages and tagged tuples
    # land in one list and the comprehension below tries to unpack a frozen
    # Message (TypeError).
    messages: list = []
    sink: list = []
    class _T:
        def append(self, m): sink.append(("t", m))
    class _E:
        def append_user_message(self, *, agent_id, message): sink.append(("u", message))
        def append(self, **k): sink.append(("e", k))
    delivered = drain_all_interjects_at_resume(coord, messages, _T(), _E(), "aid")
    assert [m.content[0].text for m in messages] == ["one", "two"]
    assert [m.content[0].text for tag, m in sink if tag == "t"] == ["one", "two"]
    assert delivered == 2
    assert coord.drain_interject() is None  # queue cleared


@pytest.mark.anyio
async def test_anthropic_resume_entry_drain_before_first_send(tmp_path: Path) -> None:
    """Interjects queued before resume must appear in the FIRST messages.create
    call, not one turn late."""
    from imp.agents.runner import (
        AgentInvocation,
        ChangeContext,
        Message,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
    )
    from imp.agents.tools import get_registry  # noqa: F401 — ensures tools registered
    from imp.agents.transcript import Transcript
    from imp.engine.state import InMemoryEventLog
    from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
    from imp.providers.anthropic import AnthropicAgentRunner
    from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use

    # Build a minimal agent def that has complete_step + read_file.
    agent_def = AgentDef(
        name="t", description="",
        tools=frozenset({"complete_step", "read_file"}),
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

    inv_dir = tmp_path / "step"
    inv_dir.mkdir(parents=True, exist_ok=True)

    # Seed a transcript that ends with a user tool_result (RESUME_NORMAL state)
    # so the provider takes the resume branch without AWAITING_USER.
    transcript = Transcript(inv_dir / "transcript.jsonl")
    transcript.append(Message(role="user", content=(TextBlock(text="resume me"),)))
    transcript.append(Message(role="assistant", content=(
        ToolUseBlock(id="u1", name="read_file", input={"path": "/project/x"}),
    )))
    transcript.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="u1", content="prior contents"),
    )))

    # Coordinator with two queued interjects.
    class _FakeCoordinator:
        def __init__(self) -> None:
            self._q = ["interject-A", "interject-B"]

        def drain_interject(self) -> str | None:
            return self._q.pop(0) if self._q else None

        def reset(self) -> None:
            pass

    coord = _FakeCoordinator()

    # Fake client: complete on the first call (so we only need one response).
    fake = FakeAsyncAnthropic([
        response(tool_use("c1", "complete_step", {"status": "Success", "note": "ok"})),
    ])
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6",
        client_factory=lambda: fake,
    )

    inv = AgentInvocation(
        agent_def=agent_def,
        provider_name="anthropic",
        model_name="claude-sonnet-4-6",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            slug="test", title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow",
        ),
        transcript_dir=inv_dir,
        fs_permissions=(FsRule(
            operations=("read",), paths=("/project/**",),
            mode="allow",
            source=SourceLoc(file=Path("w.yaml"), line=1),
        ),),
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=log,
        coordinator=coord,
    )

    events = [e async for e in runner.run(inv)]

    # Run completed successfully.
    assert events[-1]["type"] == "agent_output"

    # The FIRST messages.create call must include BOTH interject texts
    # (appended right after the 3 transcript messages).
    first_call = fake.messages.calls[0]
    first_call_messages = first_call["messages"]

    # Extract text content from all user messages in the first call.
    all_texts = []
    for m in first_call_messages:
        if m.get("role") == "user":
            for block in m.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    all_texts.append(block["text"])

    assert "interject-A" in all_texts, (
        f"interject-A missing from first request messages: {first_call_messages}"
    )
    assert "interject-B" in all_texts, (
        f"interject-B missing from first request messages: {first_call_messages}"
    )
    # No double-delivery: each interject appears EXACTLY once across the whole
    # conversation (the entry drain cleared the queue, so the later turn-boundary
    # drain is a no-op).
    all_request_texts: list[str] = []
    for call in fake.messages.calls:
        for m in call["messages"]:
            content = m["content"] if isinstance(m, dict) else m.content
            if isinstance(content, str):
                all_request_texts.append(content)
            else:
                for block in content:
                    text = block.get("text") if isinstance(block, dict) else None
                    if text is not None:
                        all_request_texts.append(text)
    assert all_request_texts.count("interject-A") == 1
    assert all_request_texts.count("interject-B") == 1
    # Both interjects consumed (queue cleared).
    assert coord.drain_interject() is None


@pytest.mark.anyio
async def test_openai_resume_entry_drain_before_first_send(tmp_path: Path) -> None:
    """Interjects queued before resume must appear in the FIRST responses.create
    call, not one turn late."""
    from imp.agents.runner import (
        AgentInvocation,
        ChangeContext,
        Message,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
    )
    from imp.agents.tools import get_registry  # noqa: F401 — ensures tools registered
    from imp.agents.transcript import Transcript
    from imp.engine.state import InMemoryEventLog
    from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
    from imp.providers.openai import OpenAIAgentRunner
    from tests.openai_fakes import FakeResponsesClient, resp_function_call, resp_response

    agent_def = AgentDef(
        name="t", description="",
        tools=frozenset({"complete_step", "read_file"}),
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

    inv_dir = tmp_path / "step"
    inv_dir.mkdir(parents=True, exist_ok=True)

    # Seed a transcript that ends with a user tool_result (RESUME_NORMAL state)
    # so the provider takes the resume branch without AWAITING_USER.
    transcript = Transcript(inv_dir / "transcript.jsonl")
    transcript.append(Message(role="user", content=(TextBlock(text="resume me"),)))
    transcript.append(Message(role="assistant", content=(
        ToolUseBlock(id="u1", name="read_file", input={"path": "/project/x"}),
    )))
    transcript.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="u1", content="prior contents"),
    )))

    # Coordinator with two queued interjects.
    class _FakeCoordinator:
        def __init__(self) -> None:
            self._q = ["interject-A", "interject-B"]

        def drain_interject(self) -> str | None:
            return self._q.pop(0) if self._q else None

        def reset(self) -> None:
            pass

    coord = _FakeCoordinator()

    # Fake client: complete on the first call (so we only need one response).
    fake = FakeResponsesClient([
        resp_response(
            resp_function_call("c1", "complete_step", '{"status": "Success"}'),
        ),
    ])
    log = InMemoryEventLog()
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini",
        client_factory=lambda: fake,
    )

    inv = AgentInvocation(
        agent_def=agent_def,
        provider_name="openai",
        model_name="gpt-4o-mini",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            slug="test", title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow",
        ),
        transcript_dir=inv_dir,
        fs_permissions=(FsRule(
            operations=("read",), paths=("/project/**",),
            mode="allow",
            source=SourceLoc(file=Path("w.yaml"), line=1),
        ),),
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=log,
        coordinator=coord,
    )

    events = [e async for e in runner.run(inv)]

    # Run completed successfully.
    assert events[-1]["type"] == "agent_output"

    # The FIRST responses.create call must include BOTH interject texts
    # (appended right after the 3 transcript messages).
    first_call = fake.responses.calls[0]
    first_call_input = first_call["input"]

    # Extract text content from all user messages in the first call.
    all_texts = []
    for item in first_call_input:
        if item.get("type") == "message" and item.get("role") == "user":
            for part in item.get("content", []):
                if isinstance(part, dict) and part.get("type") == "input_text":
                    all_texts.append(part["text"])

    assert "interject-A" in all_texts, (
        f"interject-A missing from first request input: {first_call_input}"
    )
    assert "interject-B" in all_texts, (
        f"interject-B missing from first request input: {first_call_input}"
    )
    # No double-delivery: each interject appears EXACTLY once across the whole
    # conversation (the entry drain cleared the queue, so the later turn-boundary
    # drain is a no-op).
    all_request_texts: list[str] = []
    for call in fake.responses.calls:
        for item in call["input"]:
            if item.get("type") == "message" and item.get("role") == "user":
                for part in item.get("content", []):
                    if isinstance(part, dict) and part.get("type") == "input_text":
                        all_request_texts.append(part["text"])
    assert all_request_texts.count("interject-A") == 1
    assert all_request_texts.count("interject-B") == 1
    # Both interjects consumed (queue cleared).
    assert coord.drain_interject() is None


@pytest.mark.anyio
async def test_claude_code_resume_entry_drain_before_first_query(
    tmp_path: Path,
) -> None:
    """Interjects queued before resume must appear in the FIRST client.query()
    payload (the streamed history), not one turn late."""
    from typing import Any

    from imp.agents.runner import (
        AgentInvocation,
        ChangeContext,
        Message,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
    )
    from imp.agents.tools import get_registry  # noqa: F401 — ensures tools registered
    from imp.agents.transcript import Transcript
    from imp.engine.state import InMemoryEventLog
    from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
    from imp.providers.claude_code import ClaudeCodeAgentRunner
    from tests.cc_fakes import (
        FakeClaudeSDKClient,
        assistant,
        result,
        text as cc_text,
        tool_use as cc_tool_use,
    )

    agent_def = AgentDef(
        name="t", description="",
        tools=frozenset({"complete_step", "read_file"}),
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

    inv_dir = tmp_path / "step"
    inv_dir.mkdir(parents=True, exist_ok=True)

    # Seed a transcript ending with a user tool_result — RESUME_NORMAL state
    # (not AWAITING_USER), so the provider should drain interjects before the
    # first query.
    transcript = Transcript(inv_dir / "transcript.jsonl")
    transcript.append(Message(role="user", content=(TextBlock(text="resume me"),)))
    transcript.append(Message(role="assistant", content=(
        ToolUseBlock(id="u1", name="read_file", input={"path": "/project/x"}),
    )))
    transcript.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="u1", content="prior contents"),
    )))

    # Coordinator with two queued interjects.
    class _FakeCoordinator:
        def __init__(self) -> None:
            self._q = ["interject-A", "interject-B"]

        def drain_interject(self) -> str | None:
            return self._q.pop(0) if self._q else None

        def reset(self) -> None:
            pass

    coord = _FakeCoordinator()

    # Scripted: complete on the first call.
    scripted = [
        assistant(
            cc_text("done"),
            cc_tool_use("c1", "complete_step", {"status": "Success"}),
        ),
        result(),
    ]

    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        created.append(c)
        return c

    log = InMemoryEventLog()
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=factory,
    )

    inv = AgentInvocation(
        agent_def=agent_def,
        provider_name="claude_code",
        model_name="claude-sonnet-4-5",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            slug="test", title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow",
        ),
        transcript_dir=inv_dir,
        fs_permissions=(FsRule(
            operations=("read",), paths=("/project/**",),
            mode="allow",
            source=SourceLoc(file=Path("w.yaml"), line=1),
        ),),
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=log,
        coordinator=coord,
    )

    events = [e async for e in runner.run(inv)]

    # Run completed successfully.
    assert events[-1]["type"] == "agent_output"

    assert created, "client was never instantiated"
    fake = created[0]

    # The FIRST query is an async-iterable drain → list of stream-json dicts.
    assert fake.queries, "no queries recorded"
    first_query = fake.queries[0]
    assert isinstance(first_query, list), (
        f"expected drained list for first (history) query, got {type(first_query)}"
    )

    # Collect all text content from user-role dicts in the first query.
    def _texts_in_query(q: list) -> list[str]:
        texts: list[str] = []
        for d in q:
            if not isinstance(d, dict):
                continue
            if d.get("type") != "user":
                continue
            msg = d.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(block.get("text", ""))
        return texts

    first_query_texts = _texts_in_query(first_query)

    assert "interject-A" in first_query_texts, (
        f"interject-A missing from first query user texts: {first_query_texts}"
    )
    assert "interject-B" in first_query_texts, (
        f"interject-B missing from first query user texts: {first_query_texts}"
    )

    # No double-delivery: each interject text appears EXACTLY ONCE across all
    # queries (entry drain cleared the queue; later turn-boundary drain is no-op).
    all_texts: list[str] = []
    for q in fake.queries:
        if isinstance(q, list):
            all_texts.extend(_texts_in_query(q))
        elif isinstance(q, str):
            all_texts.append(q)

    assert all_texts.count("interject-A") == 1, (
        f"interject-A delivered {all_texts.count('interject-A')} times (expected 1): {all_texts}"
    )
    assert all_texts.count("interject-B") == 1, (
        f"interject-B delivered {all_texts.count('interject-B')} times (expected 1): {all_texts}"
    )

    # Both interjects consumed (queue cleared).
    assert coord.drain_interject() is None


@pytest.mark.anyio
async def test_codex_resume_entry_drain_folds_into_first_prompt(
    tmp_path: Path,
) -> None:
    """Interjects queued before codex resume must be folded into the FIRST
    `run_streamed(next_prompt)` call, not one turn late. Unlike the
    messages-list providers, codex re-prompts with a string, so the drain
    must append the texts to `next_prompt` before the `while True` loop."""
    import json
    import subprocess

    from imp.agents.runner import AgentInvocation, ChangeContext
    from imp.engine.state import InMemoryEventLog
    from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
    from imp.providers.codex_runner import CodexAgentRunner
    from tests.codex_fakes import (
        FakeCodex,
        agent_message,
        mcp_call,
        thread_started,
        turn_completed,
    )

    _SRC = SourceLoc(file=Path("a.md"), line=1)

    # Bootstrap a git repo so preflight() passes.
    def _git_init(p: Path) -> None:
        subprocess.run(["git", "init", "-q", str(p)], check=True)
        subprocess.run(["git", "-C", str(p), "config", "user.email", "t@t.t"], check=True)
        subprocess.run(["git", "-C", str(p), "config", "user.name", "T"], check=True)
        (p / ".gitkeep").write_text("")
        subprocess.run(["git", "-C", str(p), "add", ".gitkeep"], check=True)
        subprocess.run(["git", "-C", str(p), "commit", "-q", "-m", "init"], check=True)

    _git_init(tmp_path)
    (tmp_path / "auth.json").write_text('{"auth_mode": "chatgpt", "tokens": {}}')
    tx = tmp_path / "tx"
    tx.mkdir(exist_ok=True)

    # Pre-seed a rollout (so restore_rollout succeeds on the balance_on_resume path).
    rel = "sessions/2026/06/05/rollout-2026-06-05T00-00-00-thr-drain-1.jsonl"
    (tx / "codex_thread.json").write_text(
        json.dumps({"thread_id": "thr-drain-1", "rollout_relpath": rel}))
    saved = tx / "codex_rollout" / rel
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text('{"id": "thr-drain-1"}\n')

    # Seed events.jsonl with a complete_step tool_call so derive_codex_resume_state
    # returns SEND_TO_LLM (not AWAITING_USER, not MISSING_COMPLETE_STEP).
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps({"agent_id": "step:t", "type": "tool_call",
                    "payload": {"name": "complete_step", "input": {}}}) + "\n" +
        json.dumps({"agent_id": "step:t", "type": "llm_response",
                    "payload": {"raw": {"content": [{"type": "text", "text": "done"}]}}}) + "\n"
    )

    agent_def = AgentDef(
        name="t", description="",
        tools=frozenset({"complete_step"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="answer", type="string", required=True),),
            source=_SRC),
        prompt_template_source="", source_path=Path("a.md"),
        source=_SRC, tier=None,
    )

    # Coordinator with two queued interjects.
    class _FakeCoordinator:
        def __init__(self) -> None:
            self._q = ["interject-A", "interject-B"]

        def drain_interject(self) -> str | None:
            return self._q.pop(0) if self._q else None

        def reset(self) -> None:
            pass

    coord = _FakeCoordinator()

    # Scripted: one turn, completes with complete_step.
    scripted = [[
        thread_started("thr-drain-1"),
        agent_message("resuming"),
        mcp_call(name="complete_step", args={"answer": "ok"}, result="done", item_id="cs-1"),
        turn_completed(input_tokens=1, output_tokens=1),
    ]]
    fake = FakeCodex(scripted_turns=scripted)
    log = InMemoryEventLog()
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: fake,
    )
    runner._canonical_auth_override = tmp_path / "auth.json"

    inv = AgentInvocation(
        agent_def=agent_def,
        provider_name="codex",
        model_name="gpt-5.5",
        system_prompt="be terse",
        initial_messages=(),
        change_context=ChangeContext(
            slug="s", title="t", project_root=tmp_path,
            workflow_dir=tmp_path, id="c1"),
        transcript_dir=tx,
        fs_permissions=(FsRule(
            operations=("read", "write"), paths=("**",),
            mode="allow", source=_SRC),),
        events_path=events_path,
        agent_id="step:t",
        event_log=log,
        coordinator=coord,
        balance_on_resume=True,
    )

    events = [e async for e in runner.run(inv)]

    # Run completed successfully.
    assert events[-1]["type"] == "agent_output", events

    # There must have been exactly one run_streamed call (the first turn).
    thread = fake.thread
    assert thread is not None
    assert thread.run_calls, "run_streamed was never called"

    # The FIRST run_streamed argument must contain BOTH interject texts.
    first_prompt = thread.run_calls[0][0]
    assert isinstance(first_prompt, str), f"expected str prompt, got {type(first_prompt)}"
    assert "interject-A" in first_prompt, (
        f"interject-A missing from first prompt: {first_prompt!r}"
    )
    assert "interject-B" in first_prompt, (
        f"interject-B missing from first prompt: {first_prompt!r}"
    )

    # Each interject text must appear exactly ONCE across all run_streamed calls
    # (the entry drain cleared the queue; the turn-boundary drain is a no-op).
    all_prompts = " ".join(call[0] for call in thread.run_calls if isinstance(call[0], str))
    assert all_prompts.count("interject-A") == 1, (
        f"interject-A delivered {all_prompts.count('interject-A')} times (expected 1)"
    )
    assert all_prompts.count("interject-B") == 1, (
        f"interject-B delivered {all_prompts.count('interject-B')} times (expected 1)"
    )

    # Both interjects consumed (queue cleared).
    assert coord.drain_interject() is None

