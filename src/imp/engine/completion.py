"""Render an OutputSchema into a 'call complete_step' instruction.

Single source of truth, consumed by the shared `engine.agent_run` builder
(and through it, both the executor and dispatch_task). No provider imports
this module, so there is no providers→engine import edge.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from imp.engine.feedback import COMPLETION_NUDGE
from imp.engine.types import OutputSchema


def completion_instruction(schema: OutputSchema) -> str:
    """Prose instruction naming every field the agent must pass to
    `complete_step`, with enum values inlined and optional fields marked."""
    assert schema.fields, "OutputSchema must have at least one field"
    parts: list[str] = []
    for field in schema.fields:
        if field.type == "enum":
            spec = f"`{field.name}` ({'|'.join(field.values)})"
        else:
            spec = f"`{field.name}` ({field.type})"
        if not field.required:
            spec += " (optional)"
        parts.append(spec)
    fields_text = ", ".join(parts)
    return (
        "To finish this step you MUST call the `complete_step` tool with: "
        f"{fields_text}. Structured arguments are not shown to the user, so "
        "state your conclusion as prose in the same turn that calls "
        "`complete_step` — do not give your summary in one turn and call the "
        "tool in the next, because a turn that ends on prose alone is read as "
        "yielding control, not as finishing the step."
    )


# Placeholder shown at the cooperative-interrupt steer gate (Ctrl-C mid-turn).
# The bare turn-end yield keeps an empty prompt — its preceding prose IS the
# question — but an interrupt has no such framing, so it needs to orient the
# human: redirect inline, or detach (Ctrl-D) to leave the run paused.
INTERRUPT_STEER_HINT = "interrupted — type to redirect, or Ctrl-D to leave it paused"


def _cancelled_exc() -> type[BaseException]:
    import anyio

    return anyio.get_cancelled_exc_class()


def build_inline_solicit(
    *,
    user_input_fn: Callable[[str], str | Awaitable[str]] | None = None,
    coordinator: Any = None,
    event_log: Any = None,
    agent_id: str | None = None,
    steer_prompt: str = "",
    interrupt: bool = False,
) -> Callable[[str], Awaitable[str]]:
    """The turn-end yield-to-human tail, shared by BOTH the bare-turn-end
    continuation (build_continuation_fn) and the cooperative-interrupt steer.
    Emits awaiting_user_input, then either solicits the human inline — freezing
    the inactivity watchdog for the blocked await, and turning a mid-solicit
    detach/cancel into a clean AwaitingUserInput pause — and returns the reply,
    or (no human attached) raises AwaitingUserInput(last_text).

    `steer_prompt` rides in the awaiting_user_input payload and surfaces as the
    gate placeholder. The bare turn-end leaves it "" (its prose is the question);
    the interrupt steer passes INTERRUPT_STEER_HINT so the human is oriented."""
    from imp.engine.awaiting_user_input import AwaitingUserInput

    def _emit_awaiting() -> None:
        if event_log is not None:
            event_log.append(agent_id=agent_id, type="awaiting_user_input",
                             payload={"prompt": steer_prompt})

    async def _solicit() -> str:
        assert user_input_fn is not None
        reply = user_input_fn("")
        if inspect.isawaitable(reply):
            # Attended agent is about to BLOCK on a human reply: freeze the
            # inactivity watchdog so it cannot falsely kill the parked run. The
            # finally ALWAYS restores it; a torn-down prompt (SIGINT/cancel)
            # becomes the headless AwaitingUserInput pause.
            if coordinator is not None:
                coordinator.begin_solicit_watchdog()
            try:
                reply = await reply
            except (KeyboardInterrupt, _cancelled_exc()):
                raise AwaitingUserInput(interrupt=interrupt) from None
            finally:
                if coordinator is not None:
                    coordinator.end_solicit_watchdog()
        assert isinstance(reply, str)
        return reply

    async def _inline(last_text: str) -> str:
        # Emit so codex resume reads this pause as AWAITING_USER, not MISSING_COMPLETE_STEP.
        _emit_awaiting()
        if user_input_fn is not None:
            try:
                return await _solicit()
            except AwaitingUserInput as e:
                # The headless hibernation gate raises its own AwaitingUserInput
                # (interrupt=False) when the solicit blocks then detaches; for the
                # cooperative-interrupt steer that supersedes the interrupt cause,
                # so re-stamp it True. The executor shadows it into
                # State.interrupt_pending → a no-message resume continues the step.
                if interrupt and not e.interrupt:
                    raise AwaitingUserInput(e.prompt, interrupt=True) from None
                raise
        raise AwaitingUserInput(last_text, interrupt=interrupt)

    return _inline


def build_continuation_fn(
    *,
    user_input_fn: Callable[[str], str | Awaitable[str]] | None = None,
    coordinator: Any = None,
    event_log: Any = None,
    agent_id: str | None = None,
    headless: bool = False,
    interactive: bool = False,
    has_schema: bool = False,
) -> Callable[[str], str | Awaitable[str]]:
    """End-of-turn handler for a bare turn-end (assistant text, no
    complete_step). Whether that is a question to the human or a forgotten
    complete_step is indistinguishable, so resolution turns on WHO can take the
    yield and asks the agent ONCE, never on guessing intent:

      1. A pending interject (a line the human typed mid-run) satisfies the
         yield — return it as the next user message (and clears the one-shot
         nudge bit so a later yield gets its own nudge).
      2. One-shot disambiguation nudge (schema-bearing, non-interactive-flag agents
         only): the FIRST bare turn-end of a yield returns COMPLETION_NUDGE as
         the next user message — asking the agent to call complete_step or reply
         WAITING — and emits a completion_nudge event. Fires at most once per
         yield; behavior keys only on whether the next turn calls complete_step.
      3. Already nudged, or nudge not applicable (interactive steps, or schema-less
         agents — a true subagent's bare turn-end is its deliverable and never
         reaches this handler): today's yield behavior, carrying the stashed pre-nudge
         text so the human sees the original question, not the post-nudge reply:
           a. Attended (user_input_fn present and not headless): a human
              is here, so SOLICIT inline and let them decide; the reply becomes
              the next user message. The inactivity watchdog is frozen for the
              blocked await; a human who detaches mid-turn is handled by the
              channel itself (it raises AwaitingUserInput when no client is
              attached).
           b. No live human (headless top-level): solicit the channel if present
              (delivers a --message, else pauses via AwaitingUserInput from the
              channel), or with no channel raise AwaitingUserInput carrying the
              text to park the run.
    """
    from imp.engine.awaiting_user_input import AwaitingUserInput

    inline = build_inline_solicit(
        user_input_fn=user_input_fn, coordinator=coordinator,
        event_log=event_log, agent_id=agent_id)

    def _emit_awaiting() -> None:
        if event_log is not None:
            event_log.append(agent_id=agent_id, type="awaiting_user_input",
                             payload={"prompt": ""})

    def _emit_nudge() -> None:
        if event_log is not None:
            event_log.append(agent_id=agent_id, type="completion_nudge",
                             payload={"message": COMPLETION_NUDGE})

    nudged = False
    pre_nudge_text = ""

    def _cont(last_text: str) -> str | Awaitable[str]:
        nonlocal nudged, pre_nudge_text
        if coordinator is not None:
            pending: str | None = coordinator.drain_interject()
            if pending is not None:
                nudged = False
                return pending
        # One-shot disambiguation nudge: a schema-bearing, non-interactive-flag agent's
        # bare turn-end is indistinguishable from a forgotten complete_step, so
        # ask ONCE (WAITING or complete_step). Behavior keys only on whether the
        # NEXT turn calls complete_step; the returned string is re-queried as the
        # next user message and the marker event is emitted alongside it.
        if has_schema and not interactive and not nudged:
            nudged = True
            pre_nudge_text = last_text
            _emit_nudge()
            return COMPLETION_NUDGE
        # Already nudged (or nudge not applicable): today's behavior. When we
        # nudged, the human/park must see the ORIGINAL question, not the
        # post-nudge reply, so pass the stashed pre-nudge text. Clear the bit so
        # a later separate yield in this session gets its own single nudge.
        park_text = pre_nudge_text if nudged else last_text
        nudged = False
        if user_input_fn is not None and not headless:
            return inline(park_text)
        # Headless top-level: solicit the channel if present, else park.
        # Synchronous raise (not the async inline tail) because callers invoke
        # _cont without awaiting and rely on the raise to pause the run.
        if user_input_fn is not None:
            return inline(park_text)
        _emit_awaiting()
        raise AwaitingUserInput(park_text)

    return _cont
