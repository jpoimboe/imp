from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from imp.cli.signals import InterruptCoordinator
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.engine.completion import (
    NUDGE_LIMIT,
    build_continuation_fn,
    build_inline_solicit,
    completion_instruction,
)
from imp.engine.types import OutputField, OutputSchema, SourceLoc


def _schema() -> OutputSchema:
    return OutputSchema(
        fields=(
            OutputField(name="status", type="enum",
                        values=("Success", "Error"), required=True),
            OutputField(name="verdict", type="enum",
                        values=("approve", "rework"), required=True),
            OutputField(name="notes", type="string", required=False),
        ),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


def test_names_the_tool() -> None:
    assert "complete_step" in completion_instruction(_schema())


def test_names_every_required_field_and_enum_value() -> None:
    text = completion_instruction(_schema())
    for token in ("status", "Success", "Error", "verdict", "approve", "rework"):
        assert token in text, token


def test_marks_optional_fields_optional() -> None:
    text = completion_instruction(_schema())
    assert "notes" in text
    assert "optional" in text.lower()


def test_instructs_prose_before_tool_call() -> None:
    text = completion_instruction(_schema())
    assert "prose" in text.lower()


def test_renders_non_enum_types() -> None:
    schema = OutputSchema(
        fields=(
            OutputField(name="status", type="enum",
                        values=("Success", "Error"), required=True),
            OutputField(name="count", type="int", required=True),
            OutputField(name="found", type="bool", required=True),
            OutputField(name="payload", type="object", required=False),
        ),
        source=SourceLoc(file=Path("a.md"), line=1),
    )
    text = completion_instruction(schema)
    assert "`count` (int)" in text
    assert "`found` (bool)" in text
    assert "`payload` (object)" in text
    assert "optional" in text.lower()  # payload is optional


def _instruction() -> str:
    return "To finish this step you MUST call the `complete_step` tool with X."


class _RecordingLog:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def append(self, **ev: object) -> None:
        self.events.append(ev)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_subagent_nudges_then_returns_none() -> None:
    fn = build_continuation_fn(instruction=_instruction(), subagent=True)
    for _ in range(NUDGE_LIMIT):
        out = fn("still prose, no tool call")
        assert isinstance(out, str)
        assert "complete_step" in out
    # Exhausted: a subagent fails loudly to its parent via None -> agent_incomplete.
    assert fn("still prose") is None


def test_nudge_text_does_not_name_wait_for_user() -> None:
    fn = build_continuation_fn(instruction=_instruction(), subagent=True)
    out = fn("prose")
    assert isinstance(out, str)
    assert "complete_step" in out
    assert "wait_for_user" not in out


def test_top_level_no_channel_pauses_immediately() -> None:
    from imp.engine.awaiting_user_input import AwaitingUserInput
    fn = build_continuation_fn(instruction=_instruction(), subagent=False)
    # Headless top-level NEVER nudges: the bare turn-end IS the yield, so the
    # first call pauses carrying the last text (not agent_incomplete).
    with pytest.raises(AwaitingUserInput) as exc_info:
        fn("the final question")
    assert exc_info.value.prompt == "the final question"


def test_top_level_no_channel_pause_emits_awaiting_user_input() -> None:
    from imp.engine.awaiting_user_input import AwaitingUserInput
    log = _RecordingLog()
    fn = build_continuation_fn(
        instruction=_instruction(), event_log=log, agent_id="a1",
        subagent=False)
    # No nudge phase for headless top-level: the first bare turn-end pauses
    # carrying the last text AND emits the marker.
    with pytest.raises(AwaitingUserInput) as exc_info:
        fn("the final question")
    assert exc_info.value.prompt == "the final question"
    awaiting = [e for e in log.events if e["type"] == "awaiting_user_input"]
    assert len(awaiting) == 1
    assert awaiting[0]["agent_id"] == "a1"
    assert awaiting[0]["payload"] == {"prompt": ""}


def test_pending_interject_satisfies_without_soliciting() -> None:
    coord = InterruptCoordinator()
    coord.enqueue_interject("actually, stop and reconsider")
    calls = {"n": 0}

    def reply_fn(_q: str) -> str:
        calls["n"] += 1
        return "should-not-be-used"

    fn = build_continuation_fn(
        instruction=_instruction(), user_input_fn=reply_fn,
        coordinator=coord, non_interactive=False, subagent=False)
    assert fn("which database?") == "actually, stop and reconsider"
    assert calls["n"] == 0


@pytest.mark.anyio
async def test_attended_human_reply_becomes_next_message(
    anyio_backend: str,
) -> None:
    log = _RecordingLog()

    async def reply_fn(_q: str) -> str:
        return "use postgres"

    fn = build_continuation_fn(
        instruction=_instruction(), user_input_fn=reply_fn,
        coordinator=InterruptCoordinator(), event_log=log, agent_id="a1",
        non_interactive=False, subagent=False)
    out = fn("which database should I use?")
    assert inspect.isawaitable(out)
    assert await out == "use postgres"
    # Attended path emits awaiting_user_input and NEVER nudges.
    assert any(e["type"] == "awaiting_user_input" for e in log.events)


@pytest.mark.anyio
async def test_headless_with_channel_solicits_immediately(
    anyio_backend: str,
) -> None:
    log = _RecordingLog()
    calls = {"n": 0}

    async def channel(_q: str) -> str:
        calls["n"] += 1
        return "the --message reply"

    fn = build_continuation_fn(
        instruction=_instruction(), user_input_fn=channel,
        coordinator=InterruptCoordinator(), event_log=log, agent_id="a1",
        non_interactive=True, subagent=False)
    # Headless top-level NEVER nudges: the first bare turn-end solicits the
    # channel and emits awaiting_user_input.
    out = fn("still prose")
    assert inspect.isawaitable(out)
    assert await out == "the --message reply"
    assert calls["n"] == 1
    assert any(e["type"] == "awaiting_user_input" for e in log.events)


def test_inline_steer_no_channel_raises_interrupt_flag() -> None:
    """The cooperative-interrupt steer (interrupt=True) with no channel pauses
    carrying interrupt=True so the executor shadows State.interrupt_pending."""
    inline = build_inline_solicit(steer_prompt="interrupted", interrupt=True)
    with pytest.raises(AwaitingUserInput) as exc_info:
        import anyio
        anyio.run(inline, "last prose")
    assert exc_info.value.interrupt is True


def test_inline_bare_turn_end_no_channel_has_no_interrupt_flag() -> None:
    """A genuine bare-turn-end yield (interrupt=False default) never marks the
    pause as an interrupt."""
    inline = build_inline_solicit()
    with pytest.raises(AwaitingUserInput) as exc_info:
        import anyio
        anyio.run(inline, "the final question")
    assert exc_info.value.interrupt is False


@pytest.mark.anyio
async def test_inline_steer_restamps_gate_awaiting_user_input(
    anyio_backend: str,
) -> None:
    """When the steer solicits and the headless hibernation gate raises its own
    AwaitingUserInput (interrupt=False), the steer re-stamps it interrupt=True."""

    async def gate(_q: str) -> str:
        raise AwaitingUserInput("gate prompt")

    inline = build_inline_solicit(
        user_input_fn=gate, coordinator=InterruptCoordinator(),
        steer_prompt="interrupted", interrupt=True)
    with pytest.raises(AwaitingUserInput) as exc_info:
        await inline("last prose")
    assert exc_info.value.interrupt is True
    assert exc_info.value.prompt == "gate prompt"


@pytest.mark.anyio
async def test_inline_bare_turn_end_does_not_restamp_gate(
    anyio_backend: str,
) -> None:
    """A non-interrupt bare-turn-end leaves a gate-raised AwaitingUserInput as
    interrupt=False — only the interrupt steer re-stamps."""

    async def gate(_q: str) -> str:
        raise AwaitingUserInput("gate prompt")

    inline = build_inline_solicit(
        user_input_fn=gate, coordinator=InterruptCoordinator())
    with pytest.raises(AwaitingUserInput) as exc_info:
        await inline("still prose")
    assert exc_info.value.interrupt is False
