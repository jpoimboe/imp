"""A message lost as hidden thinking, driven through the claude and anthropic runners."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.agents.transcript import Transcript
from imp.cli import narrative as nv
from imp.cli.render_markdown import render_markdown
from imp.cli.render_text import render_tree
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.engine.completion import build_continuation_fn
from imp.engine.feedback import UNDELIVERED_MESSAGE_NUDGE as CORRECTION
from imp.engine.state import InMemoryEventLog
from imp.providers.anthropic import AnthropicAgentRunner
from imp.providers.claude import ClaudeAgentRunner
from imp.providers.claude_session_store import session_key, session_store_for
from tests import anthropic_fakes as af
from tests import cc_fakes as cc
from tests.conformance_adapters import build

PATHS = ("claude", "anthropic")
AGENT = "step:build/plan"
SUBAGENT = "step:build/plan/sub"
STUB = "I'll wait for your answer."
RESEND = "Which of the two readings did you mean?"
WARNING = "agent's message may not have reached you, asked it to send it again"
TOOLS = frozenset({"complete_step", "read_file"})
_DONE = {"status": "Success"}
_READ = {"path": "/project/x"}
_STEP = {"id": "s", "agent_id": None, "type": "step_started",
         "payload": {"step": "plan", "kind": "agent", "phase": "build", "model": "m",
                     "provider": "anthropic"}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _reply_blue(_prompt: str) -> str:
    return "blue"


def _claude_script(turns: tuple[str, ...]) -> list[Any]:
    out: list[Any] = []
    for i, turn in enumerate(turns):
        mid = f"m{i}"
        if turn in ("lost", "lost-parallel"):
            out += [cc.assistant(cc.thinking("a"), message_id=mid),
                    cc.assistant(cc.thinking("b"), message_id=mid),
                    cc.assistant(cc.tool_use("u1", "read_file", _READ), message_id=mid)]
            ids = ["u1"]
            if turn == "lost-parallel":
                out.append(cc.assistant(cc.tool_use("u2", "read_file", _READ), message_id=mid))
                ids.append("u2")
            out.append(cc.user(*(cc.tool_result(u, "data") for u in ids)))
        elif turn == "done":
            out += [cc.assistant(cc.tool_use("c1", "complete_step", _DONE), message_id=mid),
                    cc.result()]
        else:
            out += [cc.assistant(cc.text(turn), message_id=mid, stop_reason="end_turn"),
                    cc.result()]
    return out


def _anthropic_script(turns: tuple[str, ...]) -> list[Any]:
    out: list[Any] = []
    for turn in turns:
        if turn == "lost":
            out.append(af.response(af.thinking("a"), af.thinking("b"),
                                   af.tool_use("u1", "read_file", _READ)))
        elif turn == "done":
            out.append(af.response(af.tool_use("c1", "complete_step", _DONE)))
        else:
            out.append(af.response(af.text(turn), stop_reason="end_turn"))
    return out


@dataclass
class _Run:
    log: InMemoryEventLog
    raised: BaseException | None
    control: list[dict[str, Any]]
    correction_copies_sent: int
    correction_copies_in_transcript: int

    def corrections(self) -> list[dict[str, Any]]:
        return [e for e in self.log.read() if e["type"] == "engine_message"
                and e["payload"]["subtype"] == "undelivered_message"]

    def gates(self) -> list[str]:
        return [e["payload"]["last_agent_message"] for e in self.log.read()
                if e["type"] == "awaiting_user_input"]


async def _drive(path: str, tmp_path: Path, turns: tuple[str, ...], *,
                 log: InMemoryEventLog | None = None, seed: tuple[Message, ...] = (),
                 user_input_fn: Any = None, **inv_fields: Any) -> _Run:
    log = log if log is not None else InMemoryEventLog()
    cont = build_continuation_fn(event_log=log, agent_id=AGENT, interactive=True,
                                 has_schema=True, user_input_fn=user_input_fn,
                                 headless=user_input_fn is None)
    _, inv = build(path, tmp_path, [], tools=TOOLS, event_log=log, agent_id=AGENT,
                   continuation_fn=cont, interactive=True, user_input_fn=user_input_fn,
                   balance_on_resume=True, **inv_fields)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    transcript = Transcript(inv.transcript_dir / "transcript.jsonl")
    for message in seed:
        transcript.append(message)

    runner: Any
    clients: list[Any] = []
    fake = af.FakeAsyncAnthropic(_anthropic_script(turns))

    def claude_client(opts: Any) -> Any:
        clients.append(cc.FakeClaudeSDKClient(opts, scripted_messages=_claude_script(turns)))
        return clients[-1]

    if path == "claude":
        runner = ClaudeAgentRunner(model_name="claude-sonnet-4-5", client_factory=claude_client,
                                   user_input_fn=user_input_fn)
    else:
        runner = AnthropicAgentRunner(model_name="claude-sonnet-4-6", client_factory=lambda: fake,
                                      user_input_fn=user_input_fn)

    control: list[dict[str, Any]] = []
    raised: BaseException | None = None
    try:
        async for event in runner.run(inv):
            control.append(event)
    except AwaitingUserInput as exc:
        raised = exc

    if path == "claude":
        body = await session_store_for(inv.transcript_dir).load(
            session_key(inv.quest_context.project_root, AGENT)) or []
        sent = repr(clients[0].queries) + repr(body)
    else:
        sent = repr(fake.messages.calls[-1]["messages"])
    in_transcript = sum(
        CORRECTION in b.text for m in transcript.load() for b in m.content
        if isinstance(b, TextBlock))
    return _Run(log, raised, control, sent.count(CORRECTION), in_transcript)


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
async def test_attended_step_resends_before_the_prompt(path: str, tmp_path: Path) -> None:
    run = await _drive(path, tmp_path, ("lost", STUB, RESEND, "done"), user_input_fn=_reply_blue)
    assert run.correction_copies_sent == 1
    [event] = run.corrections()
    assert sorted(event["payload"]) == ["content", "role", "subtype"]
    assert event["payload"]["role"] == "user"
    assert event["payload"]["content"] == [{"type": "text", "text": CORRECTION}]
    assert run.gates() == [RESEND]
    events = list(run.log.read())
    resend = next(i for i, e in enumerate(events)
                  if e["type"] == "agent_message" and RESEND in repr(e["payload"]["raw"]))
    gate = next(i for i, e in enumerate(events) if e["type"] == "awaiting_user_input")
    assert events.index(event) < resend < gate
    assert run.control[-1]["type"] == "agent_result"


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
async def test_headless_step_parks_on_the_resend(path: str, tmp_path: Path) -> None:
    run = await _drive(path, tmp_path, ("lost", STUB, RESEND))
    assert run.correction_copies_sent == 1
    assert len(run.corrections()) == 1
    assert isinstance(run.raised, AwaitingUserInput)
    assert run.raised.prompt == RESEND
    assert run.gates() == [RESEND]


@pytest.mark.anyio
async def test_claude_lost_response_spanning_two_events_fires(tmp_path: Path) -> None:
    run = await _drive("claude", tmp_path, ("lost-parallel", STUB, RESEND))
    shapes = [[b["type"] for b in e["payload"]["raw"]["content"]] for e in run.log.read()
              if e["type"] == "agent_message" and e["payload"]["message_id"] == "m0"]
    assert shapes == [["thinking", "thinking", "tool_use"], ["tool_use"]]
    assert len(run.corrections()) == 1
    assert isinstance(run.raised, AwaitingUserInput)
    assert run.raised.prompt == RESEND


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
async def test_fires_across_a_flood_of_subagent_events(path: str, tmp_path: Path) -> None:
    flooded = False
    note = Message(role="user", content=(TextBlock(text="sub"),))

    def flood(record: dict[str, Any]) -> None:
        nonlocal flooded
        if flooded or record["agent_id"] != AGENT or record["type"] != "agent_message":
            return
        flooded = True
        for _ in range(400):
            log.append_user_message(agent_id=SUBAGENT, message=note, subtype="reply")
            log.append_engine_message(agent_id=SUBAGENT, message=note, subtype="continuation")
            log.append_agent_message(agent_id=SUBAGENT, provider="p", model="m", usage={},
                                     raw={"content": [{"type": "text", "text": "sub"}]},
                                     stop_reason=None)

    log = InMemoryEventLog(on_event=flood)
    run = await _drive(path, tmp_path, ("lost", STUB, RESEND), log=log)
    events = list(log.read())
    flood_at = [i for i, e in enumerate(events) if e["agent_id"] == SUBAGENT]
    responses_at = [i for i, e in enumerate(events)
                    if e["agent_id"] == AGENT and e["type"] == "agent_message"]
    assert len(flood_at) == 1200
    assert responses_at[0] < flood_at[0] and flood_at[-1] < responses_at[1]
    assert len(run.corrections()) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
async def test_a_subagent_never_fires(path: str, tmp_path: Path) -> None:
    run = await _drive(path, tmp_path, ("lost", STUB), subagent_depth=1)
    assert run.corrections() == []
    assert run.control[-1] == {"type": "agent_result", "payload": {"text": STUB}}


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
async def test_the_firing_stop_renders_as_the_spec_shows(path: str, tmp_path: Path) -> None:
    run = await _drive(path, tmp_path, ("lost", STUB, RESEND))
    events = [_STEP, *run.log.read()]
    tree = nv.build_tree(events)
    lean = list(render_tree(tree, full=False, show=None, color=False, width=80))
    start = lean.index(f"• {STUB}")
    assert lean[start:start + 5] == [f"• {STUB}", "", f"warning: {WARNING}", "", f"• {RESEND}"]
    assert lean.count(f"warning: {WARNING}") == 1
    assert CORRECTION[:30] not in "\n".join(lean)

    md = render_markdown(tree, full=False, show=None).split("\n")
    assert md.count(f"> **warning:** {WARNING}") == 1
    assert md.index(STUB) < md.index(f"> **warning:** {WARNING}") < md.index(RESEND)

    full = list(render_tree(tree, full=True, show=None, color=False, width=len(CORRECTION) + 10))
    assert full[full.index("undelivered message nudge:") + 1] == f"  {CORRECTION}"
    assert f"<summary>undelivered message nudge</summary>\n\n```\n{CORRECTION}\n```" in (
        render_markdown(tree, full=False, show=frozenset({"prompts"})))


_GO = Message(role="user", content=(TextBlock(text="go"),))
_TOOL_TURN = (
    Message(role="assistant",
            content=(ToolUseBlock(id="u1", name="read_file", input=_READ),)),
    Message(role="user", content=(ToolResultBlock(tool_use_id="u1", content="data"),)),
)
_STUB_TURN = Message(role="assistant", content=(TextBlock(text=STUB),))
_CORRECTION_TURN = Message(role="user", content=(TextBlock(text=CORRECTION),))


def _log_through_the_stub() -> InMemoryEventLog:
    log = InMemoryEventLog()
    for blocks in ([{"type": "thinking", "thinking": "a"}, {"type": "thinking", "thinking": "b"},
                    {"type": "tool_use", "id": "u1", "name": "read_file", "input": _READ}],
                   [{"type": "text", "text": STUB}]):
        log.append_agent_message(agent_id=AGENT, provider="p", model="m", usage={},
                                 raw={"content": blocks}, stop_reason=None)
    return log


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
async def test_resume_on_the_correction_sends_the_one_copy(path: str, tmp_path: Path) -> None:
    log = _log_through_the_stub()
    log.append_engine_message(agent_id=AGENT, message=_CORRECTION_TURN,
                              subtype="undelivered_message")
    log.append_engine_message(agent_id=AGENT, message=_CORRECTION_TURN, subtype="continuation")
    run = await _drive(path, tmp_path, (RESEND, "done"), log=log, user_input_fn=_reply_blue,
                       seed=(_GO, *_TOOL_TURN, _STUB_TURN, _CORRECTION_TURN))
    assert run.correction_copies_in_transcript == 1
    assert run.correction_copies_sent == 1
    assert len(run.corrections()) == 1
    assert run.gates() == [RESEND]


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
async def test_resume_on_the_stub_solicits_without_firing(path: str, tmp_path: Path) -> None:
    run = await _drive(path, tmp_path, ("done",), log=_log_through_the_stub(),
                       user_input_fn=_reply_blue, seed=(_GO, *_TOOL_TURN, _STUB_TURN))
    assert run.corrections() == []
    assert run.correction_copies_sent == 0
    assert run.gates() == [STUB]
    assert run.control[-1]["type"] == "agent_result"
