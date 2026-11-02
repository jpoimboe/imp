"""Native claude resumes through the real `claude` CLI against a local fake Messages API.

The fake logs every request body, so the asserts read the context the model would see,
including the CLI's load-time "No response requested." splice. No model cost.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import (
    AgentInvocation,
    Message,
    QuestContext,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from imp.agents.transcript import (
    RESUME_REDO_NUDGE,
    RESUME_STUB_NOTE,
    REWORK_RESULT_FRAME,
    Transcript,
    _synthetic_tool_result_content,
)
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.paths import project_state_dir
from imp.providers import claude
from imp.providers import claude_token_refresh as ctr
from imp.providers.claude import CAPTURED_TOOL_RESULTS_FILENAME, ClaudeAgentRunner
from tests._live import path_claude_skip_reason
from tests.conftest import _init_repo

pytestmark = [pytest.mark.live("claude"), pytest.mark.timeout(120)]

_MODEL = "claude-sonnet-4-5"
_FAKE_ID = "toolu_fake_"
_CLI_REJECTION = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected "
    "(eg. if it was a file edit, the new_string was NOT written to the file). STOP what "
    "you are doing and wait for the user to tell you how to proceed.")
_READ_A = ToolUseBlock(id="t1", name="mcp__imp__read_file", input={"path": "a.txt"})
_READ_B = ToolUseBlock(id="t2", name="mcp__imp__read_file", input={"path": "b.txt"})
_CS = ToolUseBlock(id="cs", name="mcp__imp__complete_step", input={"status": "Success"})


def _user(*blocks: Any) -> Message:
    return Message(role="user", content=tuple(blocks))


def _asst(*blocks: Any) -> Message:
    return Message(role="assistant", content=tuple(blocks))


def _sse(model: str, block: dict[str, Any], delta: dict[str, Any], stop: str) -> str:
    evs = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_" + uuid.uuid4().hex[:20], "type": "message", "role": "assistant",
            "model": model, "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": block}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": stop, "stop_sequence": None},
                           "usage": {"output_tokens": 5}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in evs)


class _FakeMessagesAPI(ThreadingHTTPServer):
    """Logs each POST body. A main turn gets one complete_step call, then plain text."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.bodies: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def main_turn(self) -> list[dict[str, Any]]:
        """The messages of the first request that offered imp's tools."""
        with self.lock:
            return next(b["messages"] for b in self.bodies if _offers_imp_tools(b))


def _offers_imp_tools(body: dict[str, Any]) -> bool:
    return any(t.get("name") == "mcp__imp__complete_step" for t in body.get("tools", []))


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _FakeMessagesAPI

    def log_message(self, *_a: Any) -> None:
        pass

    def _send(self, code: int, body: str, ctype: str = "application/json") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self) -> None:
        self._send(200, "")

    def do_GET(self) -> None:
        self._send(404, json.dumps({"type": "error", "error": {"type": "not_found_error"}}))

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        path = self.path.split("?")[0]
        if path.endswith("/count_tokens"):
            return self._send(200, json.dumps({"input_tokens": 100}))
        if not path.endswith("/v1/messages"):
            return self._send(404, json.dumps({"type": "error"}))
        with self.server.lock:
            self.server.bodies.append(body)
        model = body.get("model", _MODEL)
        answered = any(b.get("tool_use_id", "").startswith(_FAKE_ID)
                       for m in body["messages"] for b in _blocks(m))
        if _offers_imp_tools(body) and not answered:
            block = {"type": "tool_use", "id": _FAKE_ID + uuid.uuid4().hex[:12],
                     "name": "mcp__imp__complete_step", "input": {}}
            delta = {"type": "input_json_delta", "partial_json": '{"status": "Success"}'}
            return self._send(200, _sse(model, block, delta, "tool_use"), "text/event-stream")
        return self._send(200, _sse(model, {"type": "text", "text": ""},
                                    {"type": "text_delta", "text": "done"}, "end_turn"),
                          "text/event-stream")


def _blocks(msg: dict[str, Any]) -> list[dict[str, Any]]:
    c = msg["content"]
    return [{"type": "text", "text": c}] if isinstance(c, str) else c


def _result_text(block: dict[str, Any]) -> str:
    c = block.get("content")
    return c if isinstance(c, str) else "".join(p.get("text", "") for p in c or [])


def _results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for m in messages for b in _blocks(m) if b.get("type") == "tool_result"]


def _assert_stub_explained(messages: list[dict[str, Any]]) -> None:
    stub = next(i for i, m in enumerate(messages) if m["role"] == "assistant"
                and any(b.get("text") == "No response requested." for b in _blocks(m)))
    after = messages[stub + 1]
    assert after["role"] == "user", messages
    texts = [b["text"] for b in _blocks(after) if b.get("type") == "text"
             and not b["text"].startswith("<system-reminder>")]
    assert texts[0] == RESUME_STUB_NOTE, texts
    assert "[Tool call interrupted" not in json.dumps(messages)


def _assert_redo_without_pause(messages: list[dict[str, Any]]) -> None:
    texts = [b["text"] for m in messages for b in _blocks(m) if b.get("type") == "text"]
    assert RESUME_REDO_NUDGE in texts, texts
    assert "The run was paused" not in json.dumps(messages)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def fake_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeMessagesAPI]:
    reason = path_claude_skip_reason()
    if reason is not None:
        pytest.skip(reason)
    src = tmp_path / "auth-src"
    src.mkdir()
    # Without "scopes" the CLI reports "Not logged in" and never calls the API.
    (src / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "fake-tok", "expiresAt": 4_102_444_800_000,
        "scopes": ["user:inference"]}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(src))
    monkeypatch.setattr(claude, "refresh_canonical_token",
                        lambda *_a, **_k: ctr.RefreshOutcome(ctr.SKIPPED_FRESH))
    api = _FakeMessagesAPI()
    threading.Thread(target=api.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{api.server_port}")
    # Keeps the fake token off Anthropic's real hosts (bootstrap, telemetry, refresh).
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    yield api
    api.shutdown()
    api.server_close()


def _inv(tmp_path: Path, *msgs: Message, retry_feedback: str | None = None) -> AgentInvocation:
    _init_repo(tmp_path)
    src = SourceLoc(file=Path("a.md"), line=1)
    agent_def = AgentDef(
        name="resumer", description="",
        tools=frozenset({"complete_step", "read_file"}), subagents=(),
        output_schema=OutputSchema(fields=(OutputField(
            name="status", type="enum", values=("Success", "Error"), required=True),),
            source=src),
        prompt_template_source="hi", source_path=Path("a.md"), source=src)
    inv = AgentInvocation(
        agent_def=agent_def, provider_name="claude", model_name=_MODEL,
        system_prompt="be terse",
        initial_messages=(_user(TextBlock(text="go")),),
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow"),
        transcript_dir=tmp_path / "step", events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s", event_log=InMemoryEventLog(),
        balance_on_resume=True, retry_feedback=retry_feedback)
    inv.transcript_dir.mkdir(parents=True)
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    for m in msgs:
        t.append(m)
    return inv


async def _resume(inv: AgentInvocation) -> None:
    events = [ev async for ev in ClaudeAgentRunner(model_name=_MODEL).run(inv)]
    assert events[-1]["type"] == "agent_result", events[-1]
    assert not [e for e in events if e["type"] == "error"]


def _stored_complete_step_tail() -> list[Message]:
    return [_user(TextBlock(text="Finish the step.")), _asst(_CS),
            _user(ToolResultBlock(tool_use_id="cs", content="step complete", is_error=False)),
            _user(TextBlock(text="[Request interrupted by user]"))]


@pytest.mark.anyio
@pytest.mark.parametrize("cut", ["dangling", "cli_interrupt"])
async def test_parallel_mid_tool_interrupt_resume(
        cut: str, tmp_path: Path, fake_api: _FakeMessagesAPI) -> None:
    msgs = [_user(TextBlock(text="Read a.txt and b.txt, then finish.")),
            _asst(_READ_A), _asst(_READ_B)]
    if cut == "cli_interrupt":
        msgs += [_user(ToolResultBlock(tool_use_id="t1", content=_CLI_REJECTION, is_error=True)),
                 _user(ToolResultBlock(tool_use_id="t2", content=_CLI_REJECTION, is_error=True)),
                 _user(TextBlock(text="[Request interrupted by user for tool use]"))]
    inv = _inv(tmp_path, *msgs)
    (inv.transcript_dir / CAPTURED_TOOL_RESULTS_FILENAME).write_text(json.dumps({
        "name": "read_file", "input": {"path": "a.txt"},
        "content": "ALPHA-CONTENT\n", "is_error": False}) + "\n")
    await _resume(inv)

    messages = fake_api.main_turn()
    results = {b["tool_use_id"]: b for b in _results(messages)}
    assert _result_text(results["t1"]) == "ALPHA-CONTENT\n"
    assert not results["t1"].get("is_error")
    assert _result_text(results["t2"]) == _synthetic_tool_result_content(_READ_B.name)
    assert results["t2"]["is_error"] is True
    assert "doesn't want to proceed" not in json.dumps(messages)
    _assert_stub_explained(messages)


@pytest.mark.anyio
async def test_rework_resume_of_a_stored_complete_step(
        tmp_path: Path, fake_api: _FakeMessagesAPI) -> None:
    feedback = "REVISE: name the file you changed."
    await _resume(_inv(tmp_path, *_stored_complete_step_tail(), retry_feedback=feedback))

    messages = fake_api.main_turn()
    _assert_stub_explained(messages)
    results = _results(messages)
    assert [b["tool_use_id"] for b in results] == ["cs"]
    assert _result_text(results[0]) == REWORK_RESULT_FRAME + "\n\n" + feedback
    assert results[0]["is_error"] is True
    _assert_redo_without_pause(messages)


@pytest.mark.anyio
async def test_goto_resume_of_a_stored_complete_step(
        tmp_path: Path, fake_api: _FakeMessagesAPI) -> None:
    await _resume(_inv(tmp_path, *_stored_complete_step_tail()))

    messages = fake_api.main_turn()
    _assert_stub_explained(messages)
    results = _results(messages)
    assert [b["tool_use_id"] for b in results] == ["cs"]
    assert _result_text(results[0]) == "step complete"
    assert not results[0].get("is_error")
    _assert_redo_without_pause(messages)
