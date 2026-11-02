from __future__ import annotations

import re
from pathlib import Path

import pytest

from imp.cli.inert_resume import (
    bare_turn_end_question,
    make_buffered_solicit,
    render_inert_question,
    run_inert_resume,
)
from imp.engine.abandon import archive_conversation
from imp.engine.archive.read import rehydrate_quest
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.paths import project_state_dir
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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


_COLORED_FENCE = ("Here it is:\n\n```\n"
                  "\x1b[1;33m▌ imp\x1b[0m \x1b[36mplanning\x1b[0m · "
                  "\x1b[2mstep 3 of 7 · writing the plan summary\x1b[0m\n```\n")


def test_render_inert_question_keeps_fence_color_and_visible_width() -> None:
    out = render_inert_question(_COLORED_FENCE, width=70, color=True)
    hits = [ln for ln in out.split("\n")
            if "planning" in re.sub(r"\x1b\[[0-9;]*m", "", ln)]
    assert len(hits) == 1, out
    # The whole line, not just its head: today rich miscounts the escape bytes and
    # this ends at "…writing the plan", dropping "summary" onto a second line.
    assert (re.sub(r"\x1b\[[0-9;]*m", "", hits[0])
            == "  ▌ imp planning · step 3 of 7 · writing the plan summary")
    assert "\x1b[1;33m▌ imp\x1b[0m" in hits[0]
    assert hits[0].endswith("\x1b[0m")


def test_render_inert_question_caret_escapes_agent_authored_control_bytes() -> None:
    out = render_inert_question("Ready? \x1b]0;pwned\x07 now", width=80, color=True)
    assert "\x1b]" not in out
    assert "^[]0;pwned^G" in out


def test_render_inert_question_keeps_a_colored_inline_sample() -> None:
    from imp.cli.render_text import _strip_sgr

    out = render_inert_question("The gauge reads `[1;33m78%[0m` — ok?",
                                width=80, color=True)
    assert "\x1b[1;33m78%" in out
    assert "[1;33m" not in _strip_sgr(out)


def _seed_quest_with_events(
    tmp_path: Path, events: list[dict], *, status: str = "new",
) -> Quest:
    """Create a draft quest, journal `events` to its events.jsonl, save `status`."""
    _seed_control(tmp_path)
    ch = Quest.create_draft(
        tmp_path, title="C", quest_id="c", base_branch="main")
    log = ch.event_log()
    for ev in events:
        log.append(agent_id=ev.get("agent_id"), type=ev["type"],
                   payload=ev.get("payload", {}))
    State(id="c", step="s", step_attempt=0, status=status).save(ch.state_dir)
    return ch


def _gate(message: str, *, interrupt: bool = False) -> dict:
    return {"agent_id": "step:p/s", "type": "awaiting_user_input",
            "payload": {"steer_hint": "interrupted" if interrupt else "",
                        "last_agent_message": message, "interrupt": interrupt}}


def test_bare_turn_end_question_reprints_unanswered_gate_at_status_new(
        tmp_path: Path) -> None:
    # jasa11-shape: a clean bare-turn-end left at status `new` still reprints
    # (the dropped status gate no longer suppresses it).
    ch = _seed_quest_with_events(
        tmp_path, [_gate("Which DB should I use?")], status="new")
    assert bare_turn_end_question(ch) == "Which DB should I use?"


@pytest.mark.parametrize(
    "marker", ["user_message", "step_started", "step_completed"])
def test_bare_turn_end_question_none_when_answer_marker_follows(
        tmp_path: Path, marker: str) -> None:
    # A real reply (user_message) or a wait-consuming step transition after the
    # gate means the run advanced past the wait -> not parked at a prompt.
    payload: dict = {"subtype": "reply"} if marker == "user_message" else {}
    ch = _seed_quest_with_events(tmp_path, [
        _gate("Which DB?"),
        {"type": marker, "payload": payload},
    ])
    assert bare_turn_end_question(ch) is None


@pytest.mark.parametrize(
    "leave", ["conversation_saved", "conversation_archived",
              "workflow_resumed", "workflow_paused"])
def test_bare_turn_end_question_reprints_through_leave_markers(
        tmp_path: Path, leave: str) -> None:
    # save/clear/re-resume leave markers are NOT answers: the gate is still
    # unanswered, so its message reprints.
    ch = _seed_quest_with_events(tmp_path, [
        _gate("Which DB?"),
        {"type": leave, "payload": {}},
    ])
    assert bare_turn_end_question(ch) == "Which DB?"


def test_bare_turn_end_question_none_for_interrupt_gate(tmp_path: Path) -> None:
    # AC8: the last gate is a cooperative-interrupt steer (interrupt truthy) ->
    # never a reprint candidate, even though it carries last_agent_message.
    ch = _seed_quest_with_events(tmp_path, [
        _gate("mid-work prose", interrupt=True),
        {"type": "workflow_paused", "payload": {}},
    ])
    assert bare_turn_end_question(ch) is None


def test_bare_turn_end_question_uses_last_gate_only(tmp_path: Path) -> None:
    # Only the LAST awaiting_user_input decides: an earlier bare-turn-end
    # followed by a later interrupt steer resolves to "not parked".
    ch = _seed_quest_with_events(tmp_path, [
        _gate("earlier question?"),
        _gate("interrupt steer", interrupt=True),
    ])
    assert bare_turn_end_question(ch) is None


def test_bare_turn_end_question_none_without_gate(tmp_path: Path) -> None:
    # AC5: no awaiting_user_input in the tail (agent mid-work) -> no reprint.
    ch = _seed_quest_with_events(
        tmp_path, [{"type": "step_started", "payload": {"step": "s"}}])
    assert bare_turn_end_question(ch) is None


def test_bare_turn_end_question_none_when_message_empty(tmp_path: Path) -> None:
    # A gate with no recorded prose has nothing to reprint.
    ch = _seed_quest_with_events(tmp_path, [_gate("")])
    assert bare_turn_end_question(ch) is None


@pytest.mark.anyio
async def test_make_buffered_solicit_delivers_first_line_then_delegates() -> None:
    calls: list[str] = []
    seen_flags: list[bool] = []

    async def inner(prompt: str, _interrupt: bool = False) -> str:
        calls.append(prompt)
        seen_flags.append(_interrupt)
        return "from-tty"

    solicit = make_buffered_solicit("typed line", inner)

    first = await solicit("prompt-1")
    assert first == "typed line"
    assert calls == []                  # inner NOT consulted for the first reply

    second = await solicit("prompt-2")
    assert second == "from-tty"
    assert calls == ["prompt-2"]        # later replies delegate to inner

    # A delegate-path call forwards the gate interrupt flag to inner.
    third = await solicit("prompt-3", True)
    assert third == "from-tty"
    assert calls == ["prompt-2", "prompt-3"]   # every later reply delegates
    assert seen_flags == [False, True]         # the interrupt flag reaches inner


@pytest.mark.anyio
async def test_make_buffered_solicit_is_spent_by_an_interrupt_gate_without_delivering() -> None:
    # The buffered line was typed in answer to a bare turn-end question. An
    # interrupt gate is a different question, so the line is discarded and the real
    # prompt opens — and the latch is spent, so it cannot ambush a later gate either.
    calls: list[tuple[str, bool]] = []

    async def inner(prompt: str, interrupt: bool = False) -> str:
        calls.append((prompt, interrupt))
        return "from-tty"

    solicit = make_buffered_solicit("typed line", inner)

    first = await solicit("steer?", True)
    assert first == "from-tty"                  # delegated, NOT the buffered line
    assert calls == [("steer?", True)]          # and the flag reached inner

    second = await solicit("bare gate")
    assert second == "from-tty"                 # the latch is spent, not merely deferred
    assert calls == [("steer?", True), ("bare gate", False)]


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
    ch = Quest.create_draft(
        tmp_path, title="C", quest_id="c", base_branch="main")
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

    assert rc == 130
    after = ch.events_path.read_bytes() if ch.events_path.exists() else b""
    assert after == before              # launch never ran -> no events appended


def _um(subtype: str, text: str) -> dict:
    return {"agent_id": "step:p/s", "type": "user_message",
            "payload": {"role": "user", "subtype": subtype,
                        "content": [{"type": "text", "text": text}]}}


def test_reply_subtype_answers_the_gate(tmp_path: Path) -> None:
    ch = _seed_quest_with_events(tmp_path, [
        _gate("Which DB?"), _um("reply", "postgres")])
    assert bare_turn_end_question(ch) is None      # reply consumes the wait


def test_interject_subtype_answers_the_gate(tmp_path: Path) -> None:
    ch = _seed_quest_with_events(tmp_path, [
        _gate("Which DB?"), _um("interject", "actually, use sqlite")])
    assert bare_turn_end_question(ch) is None      # interject consumes the wait


def test_intent_subtype_does_not_answer_the_gate(tmp_path: Path) -> None:
    # An opening intent (never a solicited answer) after the gate must NOT count
    # as answering it — the gate is still parked and reprints its question.
    ch = _seed_quest_with_events(tmp_path, [
        _gate("Which DB?"), _um("intent", "unrelated opening")])
    assert bare_turn_end_question(ch) == "Which DB?"


def test_bare_turn_end_question_reprints_after_archive_and_rehydrate(
        tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(
        tmp_path, title="C", quest_id="c", base_branch="main")
    ch.event_log().append(
        agent_id="step:p/s", type="awaiting_user_input",
        payload={"steer_hint": "", "last_agent_message": "Which DB should I use?",
                 "interrupt": False})
    archive_conversation(ch)
    assert not (project_state_dir(tmp_path) / "quests" / "c").exists()

    rehydrate_quest(tmp_path, "c")
    restored = Quest.load(tmp_path, "c")
    # The gate is followed only by the conversation_archived leave marker that
    # archive_conversation appended -> still unanswered -> reprints.
    assert bare_turn_end_question(restored) == "Which DB should I use?"
