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
    """The last `awaiting_user_input` gate's payload when it is a bare-turn-end yield
    (`interrupt` falsy) that nothing has answered, else None."""
    events = list(quest.event_log().read())
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
