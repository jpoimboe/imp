# tests/test_claude_resume_boundary.py
"""Native-resume body/stream split: the loaded body holds every tool_result
paired behind its tool_use; the stream is only the trailing user-text run."""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.agents.transcript import (
    RESUME_CONTINUE_NUDGE,
    RESUME_DURABILITY_NOTE,
    RESUME_REDO_NUDGE,
    REWORK_RESULT_FRAME,
    Transcript,
)
from imp.engine.state import InMemoryEventLog
from imp.providers.claude import (
    CAPTURED_TOOL_RESULTS_FILENAME,
    ClaudeAgentRunner,
    _project_for_cli,
)
from imp.providers.claude_session_store import (
    message_to_session_records,
    session_key,
    session_store_for,
)
from tests.cc_fakes import FakeClaudeSDKClient, assistant, result, tool_use
from tests.test_claude_runner import _inv, _no_ts

_MODEL = "claude-sonnet-4-5"


def _u(*blocks: Any) -> Message:
    return Message(role="user", content=blocks)


def _a(*blocks: Any) -> Message:
    return Message(role="assistant", content=blocks)


def _txt(s: str) -> TextBlock:
    return TextBlock(text=s)


def _read(id_: str, path: str) -> ToolUseBlock:
    return ToolUseBlock(id=id_, name="mcp__imp__read_file", input={"path": path})


def _cs(id_: str) -> ToolUseBlock:
    return ToolUseBlock(id=id_, name="complete_step", input={"status": "Success"})


def _tr(id_: str, content: str, *, is_error: bool = False) -> ToolResultBlock:
    return ToolResultBlock(tool_use_id=id_, content=content, is_error=is_error)


_REJECTED = ("The user doesn't want to proceed with this tool use. "
             "The tool use was rejected.")
_MARKER = "[Request interrupted by user]"


@dataclass
class _Resumed:
    entered: list[Message]          # transcript.jsonl as _enter left it
    body: list[dict[str, Any]]      # store records the CLI loads
    streamed: list[dict[str, Any]]  # the stream-json delta of the first query
    session_id: str
    cwd: str


async def _resume(
    tmp_path: Path, msgs: list[Message], *,
    captured: tuple[dict[str, Any], ...] = (),
    agent_id: str = "step:p/s", user_input_fn: Any = None, **inv_fields: Any,
) -> _Resumed:
    (tmp_path / "art").mkdir()
    inv = replace(_inv(tmp_path, agent_id=agent_id, event_log=InMemoryEventLog()),
                  **inv_fields)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    for m in msgs:
        t.append(m)
    if captured:
        (inv.transcript_dir / CAPTURED_TOOL_RESULTS_FILENAME).write_text(
            "".join(json.dumps(c) + "\n" for c in captured))
    entered: list[list[Message]] = []
    created: list[FakeClaudeSDKClient] = []

    def factory(opts: Any) -> FakeClaudeSDKClient:
        entered.append(t.load())  # _enter builds the client after every repair
        c = FakeClaudeSDKClient(opts, scripted_messages=[
            assistant(tool_use("c9", "complete_step", {"status": "Success"})),
            result()])
        created.append(c)
        return c

    runner = ClaudeAgentRunner(model_name=_MODEL, client_factory=factory,
                               user_input_fn=user_input_fn)
    [ev async for ev in runner.run(inv)]
    skey = session_key(inv.quest_context.project_root, agent_id)
    body = await session_store_for(inv.transcript_dir).load(skey) or []
    return _Resumed(entered=entered[0], body=body, streamed=created[0].queries[0],
                    session_id=skey["session_id"], cwd=str(tmp_path))


def _expected_body(r: _Resumed) -> list[Message]:
    split = len(r.entered)
    while (split > 0 and r.entered[split - 1].role == "user"
           and not any(isinstance(b, ToolResultBlock)
                       for b in r.entered[split - 1].content)):
        split -= 1
    return _project_for_cli(r.entered[:split])


def _assert_boundary(r: _Resumed) -> list[Message]:
    body = _expected_body(r)
    assert _no_ts(r.body) == _no_ts(message_to_session_records(
        body, session_id=r.session_id, cwd=r.cwd, model=_MODEL))
    issued: set[str] = set()
    for i, m in enumerate(body):
        ids = [b.id for b in m.content if isinstance(b, ToolUseBlock)]
        if m.role == "assistant" and ids:
            nxt = body[i + 1] if i + 1 < len(body) else None
            assert nxt is not None and nxt.role == "user", f"unanswered {ids}"
            answered = {b.tool_use_id for b in nxt.content
                        if isinstance(b, ToolResultBlock)}
            assert set(ids) <= answered, f"unanswered {set(ids) - answered}"
        issued.update(ids)
        for b in m.content:
            if isinstance(b, ToolResultBlock):
                assert b.tool_use_id in issued, f"orphan result {b.tool_use_id}"
    assert len(r.streamed) == 1 and r.streamed[0]["type"] == "user"
    content = r.streamed[0]["message"]["content"]
    assert isinstance(content, str) or not any(
        b.get("type") == "tool_result" for b in content)
    return body


def _streamed_text(r: _Resumed) -> str:
    content = r.streamed[0]["message"]["content"]
    if isinstance(content, str):
        return content
    return "\n".join(b["text"] for b in content if b.get("type") == "text")


def _last_result(body: list[Message], tool_use_id: str) -> ToolResultBlock:
    assert body[-1].role == "user"
    hits = [b for b in body[-1].content
            if isinstance(b, ToolResultBlock) and b.tool_use_id == tool_use_id]
    assert len(hits) == 1
    return hits[0]


_PRIOR = [_u(_txt("go")), _a(_read("t0", "a.txt")), _u(_tr("t0", "ALPHA"))]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_marker_interrupt_result_is_loaded_not_streamed(tmp_path: Path) -> None:
    r = await _resume(tmp_path, [
        *_PRIOR, _a(_read("t1", "b.txt")),
        _u(_tr("t1", _REJECTED, is_error=True)), _u(_txt(_MARKER))])
    body = _assert_boundary(r)
    tr = _last_result(body, "t1")
    assert tr.is_error and tr.content != _REJECTED
    assert _streamed_text(r) == RESUME_CONTINUE_NUDGE


@pytest.mark.anyio
async def test_dangling_placeholder_is_loaded_not_streamed(tmp_path: Path) -> None:
    r = await _resume(tmp_path, [*_PRIOR, _a(_read("t1", "b.txt"))])
    body = _assert_boundary(r)
    assert _last_result(body, "t1").is_error
    assert any(isinstance(b, TextBlock) and b.text == RESUME_DURABILITY_NOTE
               for b in body[-1].content)
    assert _streamed_text(r) == RESUME_CONTINUE_NUDGE


@pytest.mark.anyio
async def test_dangling_captured_result_is_loaded(tmp_path: Path) -> None:
    r = await _resume(
        tmp_path, [*_PRIOR, _a(_read("t1", "b.txt"))],
        captured=({"name": "read_file", "input": {"path": "b.txt"},
                   "content": "REAL BODY OF b.txt", "is_error": False},))
    body = _assert_boundary(r)
    tr = _last_result(body, "t1")
    assert tr.content == "REAL BODY OF b.txt" and not tr.is_error
    assert "REAL BODY OF b.txt" not in json.dumps(r.streamed)


@pytest.mark.anyio
async def test_complete_step_rework_feedback_is_loaded(tmp_path: Path) -> None:
    r = await _resume(tmp_path, [_u(_txt("go")), _a(_cs("c0"))],
                      balance_on_resume=True, retry_feedback="REVISE: plan.md")
    body = _assert_boundary(r)
    tr = _last_result(body, "c0")
    assert tr.is_error
    assert tr.content == REWORK_RESULT_FRAME + "\n\n" + "REVISE: plan.md"
    assert "REVISE: plan.md" not in json.dumps(r.streamed)


@pytest.mark.anyio
async def test_complete_step_goto_success_is_loaded(tmp_path: Path) -> None:
    r = await _resume(tmp_path, [_u(_txt("go")), _a(_cs("c0"))],
                      balance_on_resume=True)
    body = _assert_boundary(r)
    assert not _last_result(body, "c0").is_error
    assert _streamed_text(r) == RESUME_REDO_NUDGE


@pytest.mark.parametrize("feedback", [None, "REVISE: tighten it"])
@pytest.mark.anyio
async def test_step_completed_streams_only_the_handback(
        tmp_path: Path, feedback: str | None) -> None:
    r = await _resume(
        tmp_path, [_u(_txt("go")), _a(_cs("c0")), _u(_tr("c0", "step complete")),
                   _a(_txt("all done"))],
        balance_on_resume=True, retry_feedback=feedback)
    body = _assert_boundary(r)
    assert body[-1].role == "assistant"
    assert _streamed_text(r) == (feedback or RESUME_REDO_NUDGE)


@pytest.mark.anyio
async def test_user_text_tail_streams_only_the_text(tmp_path: Path) -> None:
    r = await _resume(tmp_path, [*_PRIOR, _u(_txt("actually use b.txt"))])
    body = _assert_boundary(r)
    assert _last_result(body, "t0").content == "ALPHA"
    assert _streamed_text(r) == "actually use b.txt"


@pytest.mark.anyio
async def test_retry_feedback_over_result_tail_streams_feedback(tmp_path: Path) -> None:
    r = await _resume(tmp_path, list(_PRIOR), retry_feedback="REVISE: again")
    body = _assert_boundary(r)
    assert _last_result(body, "t0").content == "ALPHA"
    assert _streamed_text(r) == "REVISE: again"


@pytest.mark.anyio
async def test_awaiting_user_streams_the_reply(tmp_path: Path) -> None:
    r = await _resume(tmp_path, [*_PRIOR, _a(_txt("which file next?"))],
                      user_input_fn=lambda _p: "keep going")
    body = _assert_boundary(r)
    assert body[-1].role == "assistant"
    assert _streamed_text(r) == "keep going"


@pytest.mark.anyio
async def test_subagent_marker_interrupt_result_is_loaded(tmp_path: Path) -> None:
    r = await _resume(tmp_path, [
        *_PRIOR, _a(_read("t1", "b.txt")),
        _u(_tr("t1", _REJECTED, is_error=True)), _u(_txt(_MARKER))],
        agent_id="sub:tc-1", subagent_depth=1)
    body = _assert_boundary(r)
    assert _last_result(body, "t1").content != _REJECTED
    assert _streamed_text(r) == RESUME_CONTINUE_NUDGE


@pytest.mark.anyio
async def test_all_user_text_history_streams_everything(tmp_path: Path) -> None:
    r = await _resume(tmp_path, [_u(_txt("DISPATCH"))])
    assert _assert_boundary(r) == []
    assert _streamed_text(r) == "DISPATCH"
