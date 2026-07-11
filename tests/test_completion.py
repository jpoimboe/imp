from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from imp.cli.signals import InterruptCoordinator
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.engine.completion import (
    build_continuation_fn,
    build_inline_solicit,
    completion_instruction,
)
from imp.engine.feedback import COMPLETION_NUDGE
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


class _RecordingLog:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def append(self, **ev: object) -> None:
        self.events.append(ev)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_build_continuation_fn_has_no_subagent_param() -> None:
    import inspect

    from imp.engine.completion import build_continuation_fn
    params = inspect.signature(build_continuation_fn).parameters
    assert "subagent" not in params
    assert "instruction" not in params


def test_top_level_no_channel_pauses_immediately() -> None:
    from imp.engine.awaiting_user_input import AwaitingUserInput
    fn = build_continuation_fn()
    # Headless top-level NEVER nudges: the bare turn-end IS the yield, so the
    # first call pauses carrying the last text.
    with pytest.raises(AwaitingUserInput) as exc_info:
        fn("the final question")
    assert exc_info.value.prompt == "the final question"


def test_top_level_no_channel_pause_emits_awaiting_user_input() -> None:
    from imp.engine.awaiting_user_input import AwaitingUserInput
    log = _RecordingLog()
    fn = build_continuation_fn(event_log=log, agent_id="a1")
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
        user_input_fn=reply_fn, coordinator=coord, headless=False)
    assert fn("which database?") == ("actually, stop and reconsider", True)
    assert calls["n"] == 0


@pytest.mark.anyio
async def test_attended_human_reply_becomes_next_message(
    anyio_backend: str,
) -> None:
    log = _RecordingLog()

    async def reply_fn(_q: str) -> str:
        return "use postgres"

    fn = build_continuation_fn(
        user_input_fn=reply_fn, coordinator=InterruptCoordinator(),
        event_log=log, agent_id="a1", headless=False)
    text, human = fn("which database should I use?")
    assert human is True
    assert inspect.isawaitable(text)
    assert await text == "use postgres"
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
        user_input_fn=channel, coordinator=InterruptCoordinator(),
        event_log=log, agent_id="a1", headless=True)
    # Headless top-level NEVER nudges: the first bare turn-end solicits the
    # channel and emits awaiting_user_input.
    text, human = fn("still prose")
    assert human is True
    assert inspect.isawaitable(text)
    assert await text == "the --message reply"
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


def test_build_continuation_fn_has_interactive_and_has_schema_params() -> None:
    params = inspect.signature(build_continuation_fn).parameters
    assert "interactive" in params
    assert "has_schema" in params


def test_interactive_and_has_schema_do_not_change_headless_park() -> None:
    # interactive/has_schema do not alter turn-end behavior on their own:
    # a headless top-level bare turn-end still parks carrying the last text.
    fn = build_continuation_fn(interactive=True, has_schema=True)
    with pytest.raises(AwaitingUserInput) as exc_info:
        fn("the final question")
    assert exc_info.value.prompt == "the final question"


def test_first_bare_turn_end_returns_the_nudge_string() -> None:
    # A schema-bearing, non-interactive-flag bare turn-end returns the single-sourced
    # nudge (re-queried by the provider) instead of parking. Without the nudge
    # arm this call would RAISE AwaitingUserInput, so the return itself is the
    # falsifiable consequence.
    log = _RecordingLog()
    fn = build_continuation_fn(
        event_log=log, agent_id="a1", has_schema=True, interactive=False)
    assert fn("the real question") == (COMPLETION_NUDGE, False)


def test_nudge_emits_completion_nudge_event_with_message() -> None:
    log = _RecordingLog()
    fn = build_continuation_fn(
        event_log=log, agent_id="a1", has_schema=True, interactive=False)
    fn("prose without complete_step")
    nudges = [e for e in log.events if e["type"] == "completion_nudge"]
    assert len(nudges) == 1
    assert nudges[0]["agent_id"] == "a1"
    assert nudges[0]["payload"] == {"message": COMPLETION_NUDGE}


def test_second_bare_turn_end_falls_through_to_park_no_second_nudge() -> None:
    # After one nudge, a non-completion reply (another bare turn-end) is NOT
    # re-nudged: it parks (headless, no channel) exactly as today.
    log = _RecordingLog()
    fn = build_continuation_fn(
        event_log=log, agent_id="a1", has_schema=True, interactive=False)
    assert fn("the real question") == (COMPLETION_NUDGE, False)
    with pytest.raises(AwaitingUserInput):
        fn("WAITING")
    nudges = [e for e in log.events if e["type"] == "completion_nudge"]
    assert len(nudges) == 1


def test_park_after_nudge_carries_pre_nudge_text_not_reply() -> None:
    # Park fidelity: the human at the gate must see the ORIGINAL question, not
    # the post-nudge reply. Without the stash, the prompt would be "WAITING".
    fn = build_continuation_fn(has_schema=True, interactive=False)
    assert fn("the real question") == (COMPLETION_NUDGE, False)
    with pytest.raises(AwaitingUserInput) as exc_info:
        fn("WAITING")
    assert exc_info.value.prompt == "the real question"


def test_interactive_true_never_nudges_and_parks_immediately() -> None:
    log = _RecordingLog()
    fn = build_continuation_fn(
        event_log=log, agent_id="a1", has_schema=True, interactive=True)
    with pytest.raises(AwaitingUserInput) as exc_info:
        fn("a question")
    assert exc_info.value.prompt == "a question"
    assert not any(e["type"] == "completion_nudge" for e in log.events)


def test_no_schema_never_nudges_and_parks_immediately() -> None:
    # has_schema=False is the guard that keeps subagents / degenerate schema-less
    # top-level agents untouched: no nudge, park carrying the last text.
    log = _RecordingLog()
    fn = build_continuation_fn(
        event_log=log, agent_id="a1", has_schema=False, interactive=False)
    with pytest.raises(AwaitingUserInput) as exc_info:
        fn("a question")
    assert exc_info.value.prompt == "a question"
    assert not any(e["type"] == "completion_nudge" for e in log.events)


def test_pending_interject_wins_over_nudge() -> None:
    coord = InterruptCoordinator()
    coord.enqueue_interject("stop and reconsider")
    log = _RecordingLog()
    fn = build_continuation_fn(
        coordinator=coord, event_log=log, agent_id="a1",
        has_schema=True, interactive=False)
    assert fn("which database?") == ("stop and reconsider", True)
    assert not any(e["type"] == "completion_nudge" for e in log.events)


@pytest.mark.anyio
async def test_attended_first_yield_nudges_then_solicits(
    anyio_backend: str,
) -> None:
    # Attended, schema-bearing, non-interactive-flag: Part 2 replaces the old
    # "attended NEVER nudges" contract with a one-shot nudge. The FIRST bare
    # turn-end returns the nudge string (not an immediate inline solicit); a
    # subsequent bare turn-end then solicits the attended human exactly as today.
    log = _RecordingLog()

    async def reply_fn(_q: str) -> str:
        return "use postgres"

    fn = build_continuation_fn(
        user_input_fn=reply_fn, coordinator=InterruptCoordinator(),
        event_log=log, agent_id="a1", headless=False,
        has_schema=True, interactive=False)
    assert fn("which database?") == (COMPLETION_NUDGE, False)   # nudge, not a solicit
    text, human = fn("WAITING")                        # now solicits inline
    assert human is True
    assert inspect.isawaitable(text)
    assert await text == "use postgres"
    nudges = [e for e in log.events if e["type"] == "completion_nudge"]
    assert len(nudges) == 1


@pytest.mark.anyio
async def test_two_consecutive_yields_each_get_exactly_one_nudge(
    anyio_backend: str,
) -> None:
    # question -> nudge -> channel reply (run continues) -> later question ->
    # a FRESH nudge. Without clearing the nudged bit after the solicited reply,
    # the second yield would solicit instead of returning the nudge string.
    log = _RecordingLog()

    async def channel(_q: str) -> str:
        return "carry on"

    fn = build_continuation_fn(
        user_input_fn=channel, coordinator=InterruptCoordinator(),
        event_log=log, agent_id="a1", headless=True,
        has_schema=True, interactive=False)
    # Yield 1
    assert fn("question one") == (COMPLETION_NUDGE, False)
    text, human = fn("WAITING")    # falls through to channel solicit
    assert human is True
    assert inspect.isawaitable(text)
    assert await text == "carry on"
    # Yield 2 — a later, separate bare turn-end
    assert fn("question two") == (COMPLETION_NUDGE, False)
    nudges = [e for e in log.events if e["type"] == "completion_nudge"]
    assert len(nudges) == 2


def test_interject_after_nudge_clears_bit_so_later_yield_re_nudges() -> None:
    # An interject that arrives AFTER a nudge has fired must clear the one-shot
    # bit, so a later separate yield gets its OWN fresh nudge. Without the
    # `nudged = False` on the interject arm, yield 2 would fall through to a park
    # carrying the stale pre-nudge text instead of re-nudging.
    coord = InterruptCoordinator()
    log = _RecordingLog()
    fn = build_continuation_fn(
        coordinator=coord, event_log=log, agent_id="a1",
        has_schema=True, interactive=False)
    # Yield 1: nudge fires.
    assert fn("question one") == (COMPLETION_NUDGE, False)
    # A human types a mid-run interject; the next bare turn-end returns it and
    # clears the nudged bit.
    coord.enqueue_interject("actually, do X instead")
    assert fn("WAITING") == ("actually, do X instead", True)
    # Yield 2: a later separate bare turn-end must get a FRESH nudge, not a park.
    assert fn("question two") == (COMPLETION_NUDGE, False)
    nudges = [e for e in log.events if e["type"] == "completion_nudge"]
    assert len(nudges) == 2


def test_inline_steer_emit_carries_interrupt_true() -> None:
    import anyio
    log = _RecordingLog()
    inline = build_inline_solicit(
        event_log=log, agent_id="a1",
        steer_prompt="interrupted", interrupt=True)
    with pytest.raises(AwaitingUserInput):
        anyio.run(inline, "last prose")
    awaiting = [e for e in log.events if e["type"] == "awaiting_user_input"]
    assert len(awaiting) == 1
    assert awaiting[0]["payload"] == {"prompt": "interrupted", "interrupt": True}


def test_inline_bare_turn_end_emit_carries_interrupt_false() -> None:
    import anyio
    log = _RecordingLog()
    inline = build_inline_solicit(event_log=log, agent_id="a1")
    with pytest.raises(AwaitingUserInput):
        anyio.run(inline, "the final question")
    awaiting = [e for e in log.events if e["type"] == "awaiting_user_input"]
    assert len(awaiting) == 1
    assert awaiting[0]["payload"] == {"prompt": "", "interrupt": False}
