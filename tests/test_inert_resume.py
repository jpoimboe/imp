from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import Message, TextBlock, ToolUseBlock
from imp.agents.transcript import Transcript
from imp.cli.inert_resume import bare_turn_end_question, render_pending_question
from imp.engine.change import Change
from imp.engine.state import CurrentStep, State
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_render_pending_question_returns_last_assistant_text() -> None:
    msgs = [
        Message(role="user", content=(TextBlock(text="start"),)),
        Message(role="assistant", content=(TextBlock(text="Which DB should I use?"),)),
    ]
    assert render_pending_question(msgs) == "Which DB should I use?"


def test_render_pending_question_falls_back_to_preceding_prose() -> None:
    # The final assistant turn carries no text -> walk back to the prior prose.
    msgs = [
        Message(role="assistant", content=(TextBlock(text="earlier question?"),)),
        Message(role="user", content=(TextBlock(text="ok"),)),
        Message(role="assistant", content=()),
    ]
    assert render_pending_question(msgs) == "earlier question?"


def test_render_pending_question_falls_back_past_tool_use_only_turn() -> None:
    msgs = [
        Message(role="assistant", content=(TextBlock(text="earlier question?"),)),
        Message(role="user", content=(TextBlock(text="ok"),)),
        Message(role="assistant", content=(ToolUseBlock(id="t1", name="bash", input={}),)),
    ]
    assert render_pending_question(msgs) == "earlier question?"


def test_render_pending_question_none_when_no_assistant_text() -> None:
    msgs = [Message(role="user", content=(TextBlock(text="hi"),))]
    assert render_pending_question(msgs) is None


def _seed_paused_change(
    tmp_path: Path, messages: list[Message], *, status: str = "paused",
) -> Change:
    _seed_control(tmp_path)
    ch = Change.create_draft(
        tmp_path, title="C", change_id="c", base_branch="main")
    tdir = ch.change_dir / "steps" / "p" / "s"
    tdir.mkdir(parents=True)
    t = Transcript(tdir / "transcript.jsonl")
    for m in messages:
        t.append(m)
    State(id="c", step="s", step_attempt=0, status=status,
          current_step=CurrentStep(transcript_dir=tdir)).save(ch.state_dir)
    return ch


def test_bare_turn_end_question_returns_pending_question(tmp_path: Path) -> None:
    ch = _seed_paused_change(tmp_path, [
        Message(role="user", content=(TextBlock(text="start"),)),
        Message(role="assistant", content=(TextBlock(text="Which DB should I use?"),)),
    ])
    assert bare_turn_end_question(ch) == "Which DB should I use?"


def test_bare_turn_end_question_none_for_dangling_tool_use(tmp_path: Path) -> None:
    ch = _seed_paused_change(tmp_path, [
        Message(role="assistant", content=(
            TextBlock(text="running it"),
            ToolUseBlock(id="t1", name="bash", input={}),
        )),
    ])
    assert bare_turn_end_question(ch) is None


def test_bare_turn_end_question_none_when_not_paused(tmp_path: Path) -> None:
    ch = _seed_paused_change(tmp_path, [
        Message(role="assistant", content=(TextBlock(text="Which DB?"),)),
    ], status="running")
    assert bare_turn_end_question(ch) is None


def test_bare_turn_end_question_none_when_no_current_step(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="C", change_id="c", base_branch="main")
    State(id="c", step="s", step_attempt=0, status="paused").save(ch.state_dir)
    assert bare_turn_end_question(ch) is None


def test_bare_turn_end_question_none_when_transcript_absent(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="C", change_id="c", base_branch="main")
    tdir = ch.change_dir / "steps" / "p" / "s"
    tdir.mkdir(parents=True)
    State(id="c", step="s", step_attempt=0, status="paused",
          current_step=CurrentStep(transcript_dir=tdir)).save(ch.state_dir)
    assert bare_turn_end_question(ch) is None
