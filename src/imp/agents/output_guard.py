"""Shared decision point for a malformed top-level complete_step.

Returns Ok (args valid), Nudge (recoverable — provider delivers `message`
in-session so the agent re-emits), or LoopError (the invocation's LoopGuard
tripped — provider surfaces it as pause-to-human). The validation-failed turn is
fed to the guard EXACTLY ONCE here, so a provider routing through this helper must
NOT separately observe() the same turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict

from imp.agents.loop_guard import LoopGuard, TurnRecord
from imp.engine.errors import LoadError
from imp.engine.schema import validate_agent_output
from imp.engine.types import OutputSchema

if TYPE_CHECKING:
    from imp.providers.errors import LoopError


@dataclass(frozen=True)
class Ok:
    """complete_step args validated; the provider yields agent_output unchanged."""


@dataclass(frozen=True)
class Nudge:
    """A recoverable output-validation failure to deliver back to the agent."""
    message: str   # in-session text delivered to the agent to prompt a re-emit
    reason: str    # stable validation-error core (guard signature + event payload)
    attempt: int   # consecutive-failure streak, for the observability event


def build_nudge_message(reason: str) -> str:
    """Deterministic single-sourced nudge text (stable, no nonce, one guard signature)."""
    return (
        f"Your complete_step call was invalid: {reason}. "
        "Call complete_step again with all required fields present and correctly "
        "typed. Emit each field as its own separate argument; if your tool call "
        "renders as XML, close every parameter with the literal </parameter> tag "
        "(not a name-matched tag like </findings>), so no field is folded into "
        "another."
    )


def validate_output_or_nudge(
    args: dict[str, Any],
    schema: OutputSchema,
    guard: LoopGuard,
) -> Ok | Nudge | LoopError:
    """Validate a top-level complete_step's args; on failure, feed the guard once
    and decide nudge-vs-pause."""
    try:
        validate_agent_output(args, schema)
    except LoadError as e:
        reason = e.message
        record = TurnRecord(
            assistant_text="",
            tools=[],
            has_complete_step=False,
            error_category="agent_output_missing",
            error_kind="output_validation",
            error_message=reason,
        )
        loop_err = guard.observe(record)
        if loop_err is not None:
            return loop_err
        return Nudge(
            message=build_nudge_message(reason),
            reason=reason,
            attempt=guard.noprogress_streak,
        )
    return Ok()


class NudgeEvent(TypedDict):
    type: str
    payload: dict[str, object]


def make_nudge_event(nudge: Nudge) -> NudgeEvent:
    """Single-sourced nudge event payload, emitted identically by every provider."""
    return {
        "type": "output_validation_nudge",
        "payload": {"message": nudge.reason, "attempt": nudge.attempt},
    }
