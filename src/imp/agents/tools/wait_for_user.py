"""wait_for_user (Part A): the agent's explicit "yield to the human" terminator.

Attended (an input channel is attached): emit awaiting_user_input, solicit a
reply, and RETURN it as this tool's result — a normal tool result, so the
provider loop continues and the conversation is multi-turn for free. A subagent
or a run with no input channel (user_input_fn is None) emits awaiting_user_input
then raises AwaitingUserInput, which unwinds to a clean pause. A headless
top-level run IS attended (it has a one-shot --message reply channel): it
consumes its `--message` on the first solicit, then the reply fn itself raises
AwaitingUserInput on any later solicit — so the pause comes from the channel, not
from this tool. The paused wait_for_user tool_use is left DANGLING and resolved
by the REAL human reply on an attended resume — never a synthetic placeholder
(see transcript.py).
"""

from __future__ import annotations

import inspect
from typing import Any

from imp.agents.tools import Tool


class AwaitingUserInput(Exception):
    """Raised by wait_for_user when no human is attached. Propagates out of the
    provider run() (the provider re-raises it past the generic tool except,
    mirroring SubagentSuspended) and is caught in drive_invocation as a pause."""

    def __init__(self, prompt: str = "") -> None:
        super().__init__("awaiting user input")
        self.prompt = prompt


_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "The question or context to show the human.",
        }
    },
    "required": [],
    "additionalProperties": False,
}


def make_wait_for_user_tool() -> Tool:
    async def _invoke(ctx: Any, prompt: str = "", **_ignore: Any) -> str:
        if ctx.event_log is not None:
            ctx.event_log.append(
                agent_id=getattr(ctx, "agent_id", None),
                type="awaiting_user_input",
                payload={"prompt": prompt},
            )
        attended = ctx.user_input_fn is not None
        if not attended:
            raise AwaitingUserInput(prompt)
        # R6: a line the human typed WHILE the agent was running was routed to the
        # interject queue at submit time. If one is pending now, it SATISFIES this
        # wait — consume it instead of soliciting fresh input, so the human's
        # already-typed message becomes the reply and we don't block for a second.
        pending = None
        coord = getattr(ctx, "coordinator", None)
        if coord is not None:
            pending = coord.drain_interject()
        reply = pending if pending is not None else ctx.user_input_fn("")
        if inspect.isawaitable(reply):
            reply = await reply
        assert isinstance(reply, str)
        return reply

    return Tool(
        name="wait_for_user",
        description=(
            "Yield to the human and wait for their reply. Call this as your ONLY "
            "tool call in the turn. Pass `prompt` with your question. When a human "
            "is attended you receive their reply as the tool result and continue. "
            "A subagent or a run with no input channel pauses the run for a human; "
            "a headless top-level run consumes its --message reply then pauses when "
            "none remains."
        ),
        input_schema=_SCHEMA,
        fn=_invoke,
    )
