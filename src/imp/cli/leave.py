"""Lock-free leave-disposition decision for a phase-0 conversation.

The foreground decides which daemon-applied disposition a leave maps to; the
daemon applies it after it releases the per-change lock. These functions take no
lock and perform no archive/teardown — they only choose the control action.
"""
from __future__ import annotations

from pathlib import Path

from imp.engine.change import Change
from imp.engine.errors import LoadError, LoadErrors
from imp.remote.session.protocol import (
    CONTROL_ARCHIVE,
    CONTROL_DISCARD,
    CONTROL_SAVE,
)


def _had_exchange(log: object) -> bool:
    """True if the user contributed at least one message to a conversation."""
    for ev in log.read():  # type: ignore[attr-defined]
        if ev.get("type") in ("opening_intent", "user_message", "user_interjected"):
            return True
    return False


def _was_renamed(log: object) -> bool:
    """True if the change was renamed during the conversation (a design formed)."""
    return any(ev.get("type") == "change_renamed" for ev in log.read())  # type: ignore[attr-defined]


def leave_control_action(
    change: Change, leave_mode: str | None, intent: str | None,
) -> str | None:
    """Which daemon disposition frame a phase-0 leave should send.

    Returns one of CONTROL_SAVE / CONTROL_ARCHIVE / CONTROL_DISCARD, or None to
    leave the change paused (abandon has its own frame path; materialized and
    non-leave exits are left untouched).
    """
    if intent == "abandon":
        return None
    explicit_save = intent == "save"
    is_clear = intent == "clear"
    if leave_mode is None and not explicit_save and not is_clear:
        return None
    if change.materialized:
        return None
    if not explicit_save and not _had_exchange(change.event_log()):
        return CONTROL_DISCARD
    if explicit_save:
        return CONTROL_SAVE
    if is_clear:
        return CONTROL_ARCHIVE
    if leave_mode == "new":
        return CONTROL_SAVE
    if _was_renamed(change.event_log()):
        return CONTROL_SAVE
    return CONTROL_ARCHIVE


def resolve_leave_action(
    control_root: Path, change_id: str, leave_mode: str | None, intent: str | None,
) -> str | None:
    """Reload the change and decide its leave control action, or None if gone."""
    try:
        change = Change.load(control_root, change_id)
    except (LoadError, LoadErrors):
        return None
    return leave_control_action(change, leave_mode, intent)
