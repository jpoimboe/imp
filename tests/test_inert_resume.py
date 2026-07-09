from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import Message, TextBlock, ToolResultBlock, ToolUseBlock
from imp.agents.transcript import Transcript
from imp.cli.inert_resume import (
    bare_turn_end_question,
    make_buffered_solicit,
    render_inert_question,
    render_pending_question,
    run_inert_resume,
)
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


_FENCED = "Try this:\n\n```bash\ncd ~/x && make\n```\n\nThen retry."


def test_render_inert_question_renders_markdown_not_raw() -> None:
    # The fenced code block must be rendered (no literal ``` markers dumped),
    # so the replayed agent message reads as prose, not markdown source.
    out = render_inert_question(_FENCED, width=80, color=False)
    assert "```" not in out
    assert "cd ~/x && make" in out


def test_render_inert_question_emits_ansi_when_color() -> None:
    out = render_inert_question(_FENCED, width=80, color=True)
    assert "\x1b[" in out  # markdown→ANSI styling applied


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


@pytest.mark.anyio
async def test_make_buffered_solicit_delivers_first_line_then_delegates() -> None:
    calls: list[str] = []

    async def inner(prompt: str) -> str:
        calls.append(prompt)
        return "from-tty"

    solicit = make_buffered_solicit("typed line", inner)

    first = await solicit("prompt-1")
    assert first == "typed line"
    assert calls == []                  # inner NOT consulted for the first reply

    second = await solicit("prompt-2")
    assert second == "from-tty"
    assert calls == ["prompt-2"]        # later replies delegate to inner

    third = await solicit("prompt-3")
    assert third == "from-tty"
    assert calls == ["prompt-2", "prompt-3"]   # every later reply delegates


def test_render_inert_question_prefixes_turn_bullet() -> None:
    # The preview must lead with the bold "• " turn bullet on its first non-blank
    # line, matching every other agent-turn rendering.
    out = render_inert_question("Which DB should I use?", width=80, color=False)
    first = next(line for line in out.splitlines() if line.strip())
    assert first.startswith("• ")


def test_render_inert_question_hang_indents_continuation() -> None:
    # A multi-line message hangs its continuation lines 2 columns in, under the
    # first word — not flush-left and without a second bullet.
    out = render_inert_question("line one\nline two", width=80, color=False)
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[0].startswith("• line one")
    assert lines[1].startswith("  line two")
    assert not lines[1].startswith("• ")


@pytest.mark.anyio
async def test_run_inert_resume_launches_with_first_line(capsys) -> None:
    launched: list[str] = []

    async def ask_line() -> str | None:
        return "go ahead"

    async def launch(line: str) -> int:
        launched.append(line)
        return 7

    rc = await run_inert_resume(
        question="Pending Q?", ask_line=ask_line, launch=launch)

    assert rc == 7
    assert launched == ["go ahead"]
    assert "Pending Q?" in capsys.readouterr().out


@pytest.mark.anyio
async def test_run_inert_resume_blank_pads_the_question(capsys) -> None:
    # The replayed question sits between the `<id> · resuming` header and the
    # input prompt; a blank line on each side keeps it from fusing with either.
    async def ask_line() -> str | None:
        return None

    async def launch(_line: str) -> int:
        raise AssertionError("unreachable")

    await run_inert_resume(
        question="Pending Q?", ask_line=ask_line, launch=launch)

    out = capsys.readouterr().out
    assert out == "\nPending Q?\n\n"


@pytest.mark.anyio
async def test_run_inert_resume_reprompts_on_blank_line() -> None:
    lines = iter(["", "   ", "real answer"])
    launched: list[str] = []

    async def ask_line() -> str | None:
        return next(lines)

    async def launch(line: str) -> int:
        launched.append(line)
        return 0

    rc = await run_inert_resume(
        question="Pending Q?", ask_line=ask_line, launch=launch)

    assert rc == 0
    assert launched == ["real answer"]   # blank lines re-prompted, not launched


@pytest.mark.anyio
async def test_run_inert_resume_ctrl_c_leaves_change_untouched(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(
        tmp_path, title="C", change_id="c", base_branch="main")
    before = ch.events_path.read_bytes() if ch.events_path.exists() else b""

    async def ask_line() -> str | None:
        return None

    async def launch(line: str) -> int:
        # The real launch would append events; assert it never runs.
        ch.event_log().append(
            agent_id=None, type="user_message",
            payload={"role": "user", "content": []})
        return 0

    rc = await run_inert_resume(
        question="Pending Q?", ask_line=ask_line, launch=launch)

    assert rc == 0
    after = ch.events_path.read_bytes() if ch.events_path.exists() else b""
    assert after == before              # launch never ran -> no events appended


def test_bare_turn_end_question_none_for_step_completed(tmp_path: Path) -> None:
    # Trailing prose over a RESOLVED complete_step classifies STEP_COMPLETED, not
    # AWAITING_USER, so no phantom pending question is previewed.
    ch = _seed_paused_change(tmp_path, [
        Message(role="assistant", content=(
            ToolUseBlock(id="cs", name="complete_step", input={"status": "Success"}),)),
        Message(role="user", content=(
            ToolResultBlock(tool_use_id="cs", content="ok", is_error=False),)),
        Message(role="assistant", content=(TextBlock(text="All done — summary."),)),
    ])
    assert bare_turn_end_question(ch) is None
