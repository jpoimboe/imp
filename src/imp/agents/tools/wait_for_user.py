"""wait_for_user (Part A): the agent's explicit "yield to the human" terminator.

ONE path: emit awaiting_user_input, then do a single blocking read on the input
channel and RETURN its reply as this tool's result — a normal tool result, so the
provider loop continues and the conversation is multi-turn for free. There is no
attended/unattended control-flow fork: the channel is the attached human channel
when one is present (ctx.user_input_fn), else the no-human channel _headless_pause,
which raises AwaitingUserInput on read. Parking is thus a property of the CHANNEL,
not a duality inside this tool. A queued interject (a line the human typed mid-run,
R6) satisfies the wait first, regardless of which channel backs the read. A
headless top-level run is attended via its one-shot --message reply channel, which
raises AwaitingUserInput once the message is consumed. The paused wait_for_user
tool_use is left DANGLING and resolved by the REAL human reply on an attended
resume — never a synthetic placeholder (see transcript.py).
"""

from __future__ import annotations

import inspect
from typing import Any, NoReturn

from imp.agents.tools import Tool


class AwaitingUserInput(Exception):
    """Raised by a no-human input channel (_headless_pause, the headless --message
    reply fn, or the daemon's hibernation gate) to park a run at a wait_for_user
    gate. Propagates out of the provider run() (the provider re-raises it past the
    generic tool except, mirroring SubagentSuspended) and is caught in
    drive_invocation as a pause."""

    def __init__(self, prompt: str = "") -> None:
        super().__init__("awaiting user input")
        self.prompt = prompt


def _headless_pause(_query: str) -> NoReturn:
    """No-human input channel: always parks the run by raising AwaitingUserInput."""
    raise AwaitingUserInput()


def _cancelled_exc() -> type[BaseException]:
    import anyio

    return anyio.get_cancelled_exc_class()


_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}


def make_wait_for_user_tool() -> Tool:
    async def _invoke(ctx: Any, **_ignore: Any) -> str:
        if ctx.event_log is not None:
            ctx.event_log.append(
                agent_id=getattr(ctx, "agent_id", None),
                type="awaiting_user_input",
                payload={"prompt": ""},
            )
        input_fn = (
            ctx.user_input_fn if ctx.user_input_fn is not None else _headless_pause)
        # R6: a line the human typed WHILE the agent was running was routed to the
        # interject queue at submit time. If one is pending now, it SATISFIES this
        # wait — consume it instead of soliciting fresh input, so the human's
        # already-typed message becomes the reply and we don't block for a second.
        pending = None
        coord = getattr(ctx, "coordinator", None)
        if coord is not None:
            pending = coord.drain_interject()
        reply = pending if pending is not None else input_fn("")
        if inspect.isawaitable(reply):
            # Attended agent is about to BLOCK on a human reply: freeze the
            # inactivity watchdog for the duration of the await so it cannot
            # falsely kill (and re-drive) this legitimately-parked run. The
            # finally ALWAYS restores it — normal return, AwaitingUserInput, or
            # SIGINT/cancel mid-solicit — so a torn-down prompt never leaves the
            # watchdog frozen forever. (`coord` is fetched above; guard for tests/
            # legacy with no coordinator and thus no watchdog to suspend.)
            if coord is not None:
                coord.begin_solicit_watchdog()
            try:
                reply = await reply
            except (KeyboardInterrupt, _cancelled_exc()):
                # SIGTERM/escalation tore the prompt down mid-solicit. Convert to
                # the dangling-wait pause so no exception crosses the MCP boundary;
                # the headless AwaitingUserInput path takes over from here.
                raise AwaitingUserInput() from None
            finally:
                if coord is not None:
                    coord.end_solicit_watchdog()
        assert isinstance(reply, str)
        return reply

    return Tool(
        name="wait_for_user",
        description=(
            "Yield to the human and wait for their reply. Ask your question in "
            "your turn text; this tool takes no arguments. Call this as your "
            "ONLY tool call in the turn. When a human is attended you receive "
            "their reply as the tool result and continue. A subagent or a run "
            "with no input channel pauses the run for a human; a headless "
            "top-level run consumes its --message reply then pauses when none "
            "remains."
        ),
        input_schema=_SCHEMA,
        fn=_invoke,
    )
