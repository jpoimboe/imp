"""Render an OutputSchema into a 'call complete_step' instruction.

Single source of truth, consumed by the shared `engine.agent_run` builder
(and through it, both the executor and dispatch_task). No provider imports
this module, so there is no providers→engine import edge.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

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


NUDGE_LIMIT = 2


def _cancelled_exc() -> type[BaseException]:
    import anyio

    return anyio.get_cancelled_exc_class()


def build_continuation_fn(
    *,
    instruction: str,
    user_input_fn: Callable[[str], str | Awaitable[str]] | None = None,
    coordinator: Any = None,
    event_log: Any = None,
    agent_id: str | None = None,
    non_interactive: bool = False,
    subagent: bool = False,
) -> Callable[[str], str | Awaitable[str] | None]:
    """End-of-turn handler: a bare turn-end (assistant text, no complete_step)
    IS the yield to the human. Resolved at end-of-turn:

      1. A pending interject (a line the human typed mid-run) satisfies the
         yield — return it as the next user message.
      2. An interactive run (user_input_fn present and not non_interactive) is
         SOLICITED inline; the reply becomes the next user message. The
         inactivity watchdog is frozen for the blocked await. Never nudges. A
         live human who detaches mid-turn is handled by the channel itself
         (it raises AwaitingUserInput when no client is attached).
      3. No live human (headless top-level, or a subagent): nudge up to
         NUDGE_LIMIT to recover a forgotten complete_step, then on exhaustion —
           * subagent -> return None (the provider emits agent_incomplete,
             failing loudly to the parent);
           * top-level WITH a channel -> solicit it (delivers a headless
             --message, else pauses via AwaitingUserInput from the channel);
           * top-level WITHOUT a channel -> raise AwaitingUserInput carrying the
             agent's last text, pausing the run.
    """
    from imp.agents.tools.wait_for_user import AwaitingUserInput

    nudge = (
        "You ended your turn without calling `complete_step` to finish the "
        "step. " + instruction
    )
    count = 0

    def _emit_awaiting() -> None:
        if event_log is not None:
            event_log.append(agent_id=agent_id, type="awaiting_user_input",
                             payload={"prompt": ""})

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
                raise AwaitingUserInput() from None
            finally:
                if coordinator is not None:
                    coordinator.end_solicit_watchdog()
        assert isinstance(reply, str)
        return reply

    def _cont(last_text: str) -> str | Awaitable[str] | None:
        nonlocal count
        if coordinator is not None:
            pending: str | None = coordinator.drain_interject()
            if pending is not None:
                return pending
        if user_input_fn is not None and not non_interactive:
            _emit_awaiting()
            return _solicit()
        if count < NUDGE_LIMIT:
            count += 1
            return nudge
        if subagent:
            return None
        if user_input_fn is not None:
            _emit_awaiting()
            return _solicit()
        # Emit so codex resume reads this pause as AWAITING_USER, not MISSING_COMPLETE_STEP.
        _emit_awaiting()
        raise AwaitingUserInput(last_text)

    return _cont
