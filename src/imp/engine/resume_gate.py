# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Was the run parked at an unanswered question when it stopped?

Read from the fsynced event log, not state.halt_condition: run_quest wipes the
halt marker on every entry (executor.py:482), so a resume that crashes again
before re-emitting the gate would lose it, while the gate event survives. Lives
at the engine layer because both imp.cli and the session daemon consume it,
alongside the other event-log scanners (reincarnation.last_user_message,
codex_resume.pending_human_reply).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from imp.engine.quest import Quest

_ANSWER_TRANSITIONS = frozenset({"step_started", "step_completed"})
_HUMAN_ANSWER_SUBTYPES = frozenset({"reply", "interject"})


def _is_gate_answer(event: dict[str, Any]) -> bool:
    """A wait is consumed by a solicited human turn (user_message reply/interject)
    or a step transition. An opening intent is not a solicited answer."""
    etype = event.get("type")
    if etype in _ANSWER_TRANSITIONS:
        return True
    if etype == "user_message":
        return (event.get("payload") or {}).get("subtype") in _HUMAN_ANSWER_SUBTYPES
    return False


def unanswered_bare_gate(quest: Quest) -> dict[str, Any] | None:
    """`unanswered_gate_in` over the quest's event log."""
    return unanswered_gate_in(list(quest.event_log().read()))


def unanswered_gate_in(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The last `awaiting_user_input` gate's payload when it is a bare-turn-end yield
    (`interrupt` falsy) that nothing has answered, else None."""
    for i in range(len(events) - 1, -1, -1):
        if events[i].get("type") == "awaiting_user_input":
            gate = events[i]
            following = events[i + 1:]
            break
    else:
        return None
    payload: dict[str, Any] = gate.get("payload") or {}
    if payload.get("interrupt"):
        return None
    if any(_is_gate_answer(e) for e in following):
        return None
    return payload


def pending_question(events: list[dict[str, Any]]) -> str | None:
    """The unanswered bare-turn-end gate's `last_agent_message`, else None."""
    gate = unanswered_gate_in(events)
    if gate is None:
        return None
    message: str | None = gate.get("last_agent_message")
    return message or None
