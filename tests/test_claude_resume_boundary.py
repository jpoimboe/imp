# tests/test_claude_resume_boundary.py
"""Native-resume body/stream split: the loaded body holds every tool_result
paired behind its tool_use; the stream is the trailing user-text run, led by
the stub note iff the body ends on a user turn."""
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
    RESUME_STUB_NOTE,
    REWORK_RESULT_FRAME,
    Transcript,
    _synthetic_tool_result_content,
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


def _bash(id_: str, cmd: str) -> ToolUseBlock:
    return ToolUseBlock(id=id_, name="mcp__imp__bash", input={"command": cmd})


_REJECTED = ("The user doesn't want to proceed with this tool use. "
             "The tool use was rejected.")
_MARKER = "[Request interrupted by user]"
_MARKER_TOOL = "[Request interrupted by user for tool use]"


@dataclass
class _Resumed:
    entered: list[Message]          # transcript.jsonl as _enter left it
    body: list[dict[str, Any]]      # store records the CLI loads
    streamed: list[dict[str, Any]]  # the stream-json delta of the first query
    session_id: str
    cwd: str
    events: list[dict[str, Any]]    # events.jsonl after the run
    persisted: str                  # transcript.jsonl + events.jsonl text after the run


async def _resume(
    tmp_path: Path, msgs: list[Message], *,
    captured: tuple[dict[str, Any], ...] = (),
    agent_id: str = "step:p/s", user_input_fn: Any = None, **inv_fields: Any,
) -> _Resumed:
    (tmp_path / "art").mkdir()
    elog = InMemoryEventLog()
    inv = replace(_inv(tmp_path, agent_id=agent_id, event_log=elog), **inv_fields)
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
    events = list(elog.read())
    persisted = ((inv.transcript_dir / "transcript.jsonl").read_text()
                 + json.dumps(events, default=str))
    return _Resumed(entered=entered[0], body=body, streamed=created[0].queries[0],
                    session_id=skey["session_id"], cwd=str(tmp_path),
                    events=events, persisted=persisted)


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
    noted = (not isinstance(content, str)
             and content[0] == {"type": "text", "text": RESUME_STUB_NOTE})
    assert noted == bool(body and body[-1].role == "user")
    assert json.dumps(RESUME_STUB_NOTE) not in r.persisted
    assert isinstance(content, str) or not any(
        b.get("type") == "tool_result" for b in content)
    return body


def _streamed_text(r: _Resumed) -> str:
    content = r.streamed[0]["message"]["content"]
    if isinstance(content, str):
        return content
    texts = [b["text"] for b in content if b.get("type") == "text"]
    return "\n".join(texts[1:] if texts[:1] == [RESUME_STUB_NOTE] else texts)


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


@pytest.mark.parametrize("feedback", [None, "REVISE: x"])
@pytest.mark.anyio
async def test_awaiting_user_streams_the_reply(
        tmp_path: Path, feedback: str | None) -> None:
    asked: list[object] = []

    def _ask(prompt: object) -> str:
        asked.append(prompt)
        return "keep going"

    r = await _resume(tmp_path, [*_PRIOR, _a(_txt("which file next?"))],
                      user_input_fn=_ask, retry_feedback=feedback)
    body = _assert_boundary(r)
    assert body[-1].role == "assistant"
    assert _streamed_text(r) == "keep going"
    assert len(asked) == 1
    assert "REVISE: x" not in json.dumps(r.streamed)
    assert "REVISE: x" not in r.persisted


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
    assert r.streamed[0]["message"]["content"] == "DISPATCH"


@pytest.mark.anyio
async def test_fresh_spawn_streams_no_stub_note(tmp_path: Path) -> None:
    r = await _resume(tmp_path, [])
    assert [d["message"]["content"] for d in r.streamed] == ["go"]
    assert json.dumps(RESUME_STUB_NOTE) not in r.persisted


def _captured(path: str, content: str) -> dict[str, Any]:
    return {"name": "read_file", "input": {"path": path},
            "content": content, "is_error": False}


def _assert_repaired(
    r: _Resumed, *, kept: dict[str, str], placeholders: dict[str, str],
    swept: set[str], note: bool,
) -> None:
    """AC1(f): `kept` ids carry exactly that success content (captured or real),
    `placeholders` ids (-> tool name) imp's placeholder; `swept` ids alone got a
    tool_result event; the note sits in the trailing user run iff something was cut off."""
    results = {b.tool_use_id: b for m in r.entered for b in m.content
               if isinstance(b, ToolResultBlock)}
    for id_, content in kept.items():
        assert (results[id_].content, results[id_].is_error) == (content, False)
    for id_, name in placeholders.items():
        assert results[id_].content == _synthetic_tool_result_content(name)
        assert results[id_].is_error
    assert not any(_REJECTED in str(b.content) for b in results.values())
    assert not any(isinstance(b, TextBlock) and b.text in (_MARKER, _MARKER_TOOL)
                   for m in r.entered for b in m.content)
    start = len(r.entered)
    while start > 0 and r.entered[start - 1].role == "user":
        start -= 1
    notes = sum(1 for m in r.entered[start:] for b in m.content
                if isinstance(b, TextBlock) and b.text == RESUME_DURABILITY_NOTE)
    assert notes == (1 if note else 0)
    journaled = [e["payload"]["id"] for e in r.events if e["type"] == "tool_result"
                 and e["payload"]["id"] in kept.keys() | placeholders.keys()]
    assert sorted(journaled) == sorted(swept)


@pytest.mark.anyio
async def test_parallel_dangling_fragments_one_captured(tmp_path: Path) -> None:
    r = await _resume(
        tmp_path, [*_PRIOR, _a(_read("t1", "b.txt")), _a(_read("t2", "c.txt"))],
        captured=(_captured("b.txt", "REAL BODY OF b.txt"),))
    _assert_boundary(r)
    _assert_repaired(r, kept={"t1": "REAL BODY OF b.txt"},
                     placeholders={"t2": "mcp__imp__read_file"},
                     swept={"t1", "t2"}, note=True)
    assert _streamed_text(r) == RESUME_CONTINUE_NUDGE


@pytest.mark.anyio
async def test_parallel_fragments_with_one_result_under_marker(tmp_path: Path) -> None:
    r = await _resume(tmp_path, [
        *_PRIOR, _a(_read("t1", "b.txt")), _a(_read("t2", "c.txt")),
        _u(_tr("t2", "CEE")), _u(_txt(_MARKER))])
    _assert_boundary(r)
    _assert_repaired(r, kept={"t2": "CEE"},
                     placeholders={"t1": "mcp__imp__read_file"},
                     swept={"t1"}, note=True)
    assert _streamed_text(r) == RESUME_CONTINUE_NUDGE


_PER_BLOCK_INTERRUPT = [
    *_PRIOR, _a(_read("t1", "b.txt")), _a(_bash("t2", "make")),
    _u(_tr("t1", _REJECTED, is_error=True)), _u(_tr("t2", _REJECTED, is_error=True)),
    _u(_txt(_MARKER_TOOL))]


@pytest.mark.anyio
async def test_per_block_cli_interrupt_restates_every_rejection(tmp_path: Path) -> None:
    r = await _resume(tmp_path, list(_PER_BLOCK_INTERRUPT),
                      captured=(_captured("b.txt", "REAL BODY OF b.txt"),))
    _assert_boundary(r)
    _assert_repaired(r, kept={"t1": "REAL BODY OF b.txt"},
                     placeholders={"t2": "mcp__imp__bash"}, swept=set(), note=True)
    assert _streamed_text(r) == RESUME_CONTINUE_NUDGE


@pytest.mark.anyio
async def test_per_block_cli_interrupt_keeps_human_text_after_marker(
        tmp_path: Path) -> None:
    r = await _resume(tmp_path, [*_PER_BLOCK_INTERRUPT, _u(_txt("sandbox is off"))],
                      captured=(_captured("b.txt", "REAL BODY OF b.txt"),))
    _assert_boundary(r)
    _assert_repaired(r, kept={"t1": "REAL BODY OF b.txt"},
                     placeholders={"t2": "mcp__imp__bash"}, swept=set(), note=True)
    assert r.entered[-1] == _u(_txt("sandbox is off"))
    assert _streamed_text(r) == "sandbox is off"


@pytest.mark.anyio
async def test_interleaved_fragments_answer_the_earlier_call(tmp_path: Path) -> None:
    r = await _resume(tmp_path, [
        *_PRIOR, _a(_read("t1", "b.txt")), _a(_read("t2", "c.txt")),
        _u(_tr("t2", "CEE")), _a(_read("t3", "d.txt"))])
    _assert_boundary(r)
    _assert_repaired(r, kept={"t2": "CEE"},
                     placeholders={"t1": "mcp__imp__read_file",
                                   "t3": "mcp__imp__read_file"},
                     swept={"t1", "t3"}, note=True)
    assert _streamed_text(r) == RESUME_CONTINUE_NUDGE


@pytest.mark.parametrize("feedback", [None, "REVISE: use d.txt"])
@pytest.mark.anyio
async def test_dangling_call_under_several_user_turns(
        tmp_path: Path, feedback: str | None) -> None:
    r = await _resume(tmp_path, [
        *_PRIOR, _a(_read("t1", "b.txt")), _u(_txt(_MARKER)),
        _u(_txt("CORRECTION: b.txt finished")), _u(_txt("use c.txt"))],
        retry_feedback=feedback)
    body = _assert_boundary(r)
    _assert_repaired(r, kept={}, placeholders={"t1": "mcp__imp__read_file"},
                     swept={"t1"}, note=True)
    assert any(isinstance(b, TextBlock) and b.text == "CORRECTION: b.txt finished"
               for b in body[-1].content)
    assert _streamed_text(r) == "\n".join(
        ["use c.txt", *([feedback] if feedback else [])])


@pytest.mark.parametrize("feedback", [None, "REVISE: use c.txt"])
@pytest.mark.anyio
async def test_prose_over_unanswered_call_resumes_as_send_to_llm(
        tmp_path: Path, feedback: str | None) -> None:
    asked: list[Any] = []
    r = await _resume(
        tmp_path, [*_PRIOR, _a(_read("t1", "b.txt")), _a(_txt("checking b.txt"))],
        user_input_fn=lambda p: asked.append(p) or "reply",
        retry_feedback=feedback)
    body = _assert_boundary(r)
    _assert_repaired(r, kept={}, placeholders={"t1": "mcp__imp__read_file"},
                     swept={"t1"}, note=True)
    assert body[-1].role == "user" and asked == []
    assert _streamed_text(r) == (feedback or RESUME_CONTINUE_NUDGE)


@pytest.mark.anyio
async def test_dangling_tail_delivers_retry_feedback(tmp_path: Path) -> None:
    r = await _resume(
        tmp_path, [*_PRIOR, _a(_read("t1", "b.txt"))],
        retry_feedback="REVISE: use c.txt")
    _assert_boundary(r)
    _assert_repaired(r, kept={}, placeholders={"t1": "mcp__imp__read_file"},
                     swept={"t1"}, note=True)
    assert _streamed_text(r) == "REVISE: use c.txt"


@pytest.mark.anyio
async def test_success_under_marker_gets_no_note(tmp_path: Path) -> None:
    r = await _resume(tmp_path, [
        *_PRIOR, _a(_read("t1", "b.txt")), _u(_tr("t1", "BEE")), _u(_txt(_MARKER))])
    _assert_boundary(r)
    _assert_repaired(r, kept={"t1": "BEE"}, placeholders={}, swept=set(), note=False)
    assert _streamed_text(r) == RESUME_CONTINUE_NUDGE


@pytest.mark.anyio
async def test_subagent_prose_over_unanswered_call_resumes(tmp_path: Path) -> None:
    r = await _resume(tmp_path, [
        *_PRIOR, _a(_read("t1", "b.txt")), _a(_txt("partial findings")),
        _u(_txt(_MARKER))], agent_id="sub:tc-1", subagent_depth=1)
    _assert_boundary(r)
    _assert_repaired(r, kept={}, placeholders={"t1": "mcp__imp__read_file"},
                     swept={"t1"}, note=True)
    assert _streamed_text(r) == RESUME_CONTINUE_NUDGE


def _trailing_run(msgs: list[Message]) -> list[Message]:
    start = len(msgs)
    while start > 0 and msgs[start - 1].role == "user":
        start -= 1
    return msgs[start:]


def _framed(feedback: str) -> str:
    return REWORK_RESULT_FRAME + "\n\n" + feedback


def _assert_cs_tail(r: _Resumed, cs_id: str, *, result: tuple[object, bool],
                    streamed: list[str], feedback: str | None, changed: bool) -> None:
    """AC1(e): cs's only result is `result`; no durability note, no marker; the stream
    is the stub note then `streamed`; the redo nudge trails once; the answer is
    journaled as an engine continuation iff it changed, never as a tool_result
    event."""
    results = [b for m in r.entered for b in m.content
               if isinstance(b, ToolResultBlock) and b.tool_use_id == cs_id]
    assert [(b.content, b.is_error) for b in results] == [result]
    run = _trailing_run(r.entered)
    assert not any(isinstance(b, TextBlock) and b.text == RESUME_DURABILITY_NOTE
                   for m in run for b in m.content)
    assert not any(isinstance(b, TextBlock) and b.text in (_MARKER, _MARKER_TOOL)
                   for m in r.entered for b in m.content)
    assert [b["text"] for b in r.streamed[0]["message"]["content"]] == [
        RESUME_STUB_NOTE, *streamed]
    assert sum(isinstance(b, TextBlock) and b.text == RESUME_REDO_NUDGE
               for m in run for b in m.content) == 1
    if feedback:
        assert feedback not in json.dumps(r.streamed)
    assert not any(e["type"] == "tool_result" and e["payload"]["id"] == cs_id
                   for e in r.events)
    answers = [e for e in r.events if e["type"] == "engine_message" and any(
        b.get("type") == "tool_result" and b.get("tool_use_id") == cs_id
        for b in e["payload"]["content"])]
    assert len(answers) == (1 if changed else 0)
    assert _assert_boundary(r)[-1].role == "user"


_STORED_CS = [_u(_txt("go")), _a(_cs("c0")), _u(_tr("c0", "step complete")),
              _u(_txt(_MARKER))]


def _cs_expected(feedback: str | None, goto: tuple[object, bool],
                 ) -> tuple[tuple[object, bool], bool]:
    return ((_framed(feedback), True), True) if feedback else (goto, goto[0] != "step complete")


@pytest.mark.parametrize("feedback", [None, "REVISE: fix X"])
@pytest.mark.anyio
async def test_stored_complete_step_tail(tmp_path: Path, feedback: str | None) -> None:
    r = await _resume(tmp_path, list(_STORED_CS),
                      balance_on_resume=True, retry_feedback=feedback)
    result, changed = _cs_expected(feedback, ("step complete", False))
    _assert_cs_tail(r, "c0", result=result, streamed=[RESUME_REDO_NUDGE],
                    feedback=feedback, changed=changed)


@pytest.mark.parametrize("feedback", [None, "REVISE: fix X"])
@pytest.mark.anyio
async def test_stored_complete_step_tail_keeps_earlier_text(
        tmp_path: Path, feedback: str | None) -> None:
    r = await _resume(tmp_path, [*_STORED_CS, _u(_txt("OLD FEEDBACK"))],
                      balance_on_resume=True, retry_feedback=feedback)
    result, changed = _cs_expected(feedback, ("step complete", False))
    _assert_cs_tail(r, "c0", result=result,
                    streamed=["OLD FEEDBACK", RESUME_REDO_NUDGE],
                    feedback=feedback, changed=changed)


_OK = ("previous step output recorded; resuming", False)


@pytest.mark.parametrize("feedback", [None, "REVISE: fix X"])
@pytest.mark.anyio
async def test_dangling_complete_step_race(tmp_path: Path, feedback: str | None) -> None:
    r = await _resume(tmp_path, [_u(_txt("go")), _a(_cs("c0"))],
                      balance_on_resume=True, retry_feedback=feedback)
    result, changed = _cs_expected(feedback, _OK)
    _assert_cs_tail(r, "c0", result=result, streamed=[RESUME_REDO_NUDGE],
                    feedback=feedback, changed=changed)


@pytest.mark.parametrize("feedback", [None, "REVISE: fix X"])
@pytest.mark.anyio
async def test_rejected_complete_step_race(tmp_path: Path, feedback: str | None) -> None:
    r = await _resume(tmp_path, [
        _u(_txt("go")), _a(_cs("c0")), _u(_tr("c0", _REJECTED, is_error=True)),
        _u(_txt(_MARKER_TOOL))], balance_on_resume=True, retry_feedback=feedback)
    result, changed = _cs_expected(feedback, _OK)
    _assert_cs_tail(r, "c0", result=result, streamed=[RESUME_REDO_NUDGE],
                    feedback=feedback, changed=changed)


@pytest.mark.parametrize(("feedback", "kept"), [
    ("F1", "F1"), ("F2", "F2"), (None, "F1")])
@pytest.mark.anyio
async def test_complete_step_re_resume(
        tmp_path: Path, feedback: str | None, kept: str) -> None:
    r = await _resume(tmp_path, [
        _u(_txt("go")), _a(_cs("c0")), _u(_tr("c0", _framed("F1"), is_error=True)),
        _u(_txt(RESUME_REDO_NUDGE))], balance_on_resume=True, retry_feedback=feedback)
    _assert_cs_tail(r, "c0", result=(_framed(kept), True), streamed=[RESUME_REDO_NUDGE],
                    feedback=feedback, changed=kept != "F1")


@pytest.mark.anyio
async def test_complete_step_rework_sweeps_an_earlier_call(tmp_path: Path) -> None:
    fb = "REVISE: fix X"
    r = await _resume(tmp_path, [*_PRIOR, _a(_read("t1", "b.txt")), _a(_cs("c0"))],
                      balance_on_resume=True, retry_feedback=fb)
    _assert_boundary(r)
    results = {b.tool_use_id: b for m in r.entered for b in m.content
               if isinstance(b, ToolResultBlock)}
    assert (results["c0"].content, results["c0"].is_error) == (_framed(fb), True)
    assert results["t1"].content == _synthetic_tool_result_content("mcp__imp__read_file")
    assert results["t1"].is_error
    run = _trailing_run(r.entered)
    assert sum(isinstance(b, TextBlock) and b.text == RESUME_DURABILITY_NOTE
               for m in run for b in m.content) == 1
    assert [e["payload"]["id"] for e in r.events if e["type"] == "tool_result"
            and e["payload"]["id"] in ("t1", "c0")] == ["t1"]
    assert _streamed_text(r) == RESUME_REDO_NUDGE
    assert fb not in json.dumps(r.streamed)
