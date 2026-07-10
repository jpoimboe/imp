from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_log = logging.getLogger("imp.daemon")


def socket_path(control_root: Path) -> Path:
    return control_root / ".imp" / "remote-control.sock"


@dataclass(frozen=True)
class QuestInfo:
    """One Change as the `list` control verb reports it (spec control surface)."""
    id: str
    title: str
    status: str
    step: str


def list_quests(control_root: Path) -> list[QuestInfo]:
    """The in-flight Changes the `list` verb reports — those a user can still act on.

    `_iter_quest_ids` enumerates every known Change including the journal
    (completed / merged / abandoned), but those are terminal: surfacing them in a
    remote `/list` is just noise. Keep only the in-flight statuses
    (running / new / paused / merge_blocked / draft); `running` and
    `draft` are derived at display time — there is no persisted `errored`."""
    from imp.cli.main import _INFLIGHT, _iter_quest_ids, _row_for_id
    out: list[QuestInfo] = []
    for quest_id in _iter_quest_ids(control_root):
        try:
            row = _row_for_id(control_root, quest_id)
        except Exception:
            # A single mid-write or corrupt change home must never blind the
            # whole directory — that would drop every resident session's relay
            # and (in the daemon's scan loop) crash the daemon. Skip it; a later
            # poll re-reads it once it settles.
            _log.warning("list_quests: skipping %s (row read failed)",
                         quest_id, exc_info=True)
            continue
        if row is None:
            continue
        quest_id_, status_, step_, _last, title_, _created, _step_label, _phase_label = row
        if status_ not in _INFLIGHT:
            continue
        out.append(QuestInfo(id=quest_id_, title=title_, status=status_, step=step_))
    return out


def snapshot(control_root: Path, quest_id: str) -> tuple[str, str, str] | None:
    from imp.cli.main import _row_for_id
    try:
        row = _row_for_id(control_root, quest_id)
    except Exception:
        # snapshot() feeds the relay's title_for from inside a task-group child,
        # so a raise here would escape the supervisor's poll-loop guard and crash
        # the daemon. Treat a transient/corrupt read as "no snapshot".
        _log.warning("snapshot: %s row read failed", quest_id, exc_info=True)
        return None
    if row is None:
        return None
    _id, status, step, _last, title, _created, _step_label, _phase_label = row
    return (status, step, title)


def recent_narrative(control_root: Path, quest_id: str, n: int) -> list[dict[str, Any]]:
    """Last n non-empty llm_response events, oldest-first, augmented for /replay."""
    from imp.cli.main import _load_events_for_id
    from imp.remote.curate import EventCurator
    from imp.remote.drivers.telegram import event_body
    if n <= 0:
        return []
    events = _load_events_for_id(control_root, quest_id)
    if events is None:
        return []
    selected = [
        event for event in events
        if event.get("type") == "llm_response"
        and event_body("llm_response", event.get("payload") or {}).strip()
    ]
    snap = snapshot(control_root, quest_id)
    title = snap[2] if snap is not None else ""
    curator = EventCurator(id=quest_id, title_provider=lambda: title, events=("all",))
    out: list[dict[str, Any]] = []
    for event in selected[-n:]:
        augmented = curator.curate(event)
        if augmented is not None:
            out.append(augmented)
    return out


def socket_is_live(path: Path | str) -> bool:
    """True iff a daemon is accepting on path; an absent path or unlistened socket is not live."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(path))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def full_history(control_root: Path, quest_id: str) -> list[dict[str, Any]]:
    """Complete events.jsonl as raw, ordered augmented events; empty when the id is unknown."""
    from imp.cli.main import _load_events_for_id
    from imp.remote.curate import EventCurator
    events = _load_events_for_id(control_root, quest_id)
    if events is None:
        return []
    snap = snapshot(control_root, quest_id)
    title = snap[2] if snap is not None else ""
    curator = EventCurator(id=quest_id, title_provider=lambda: title, events=("all",))
    out: list[dict[str, Any]] = []
    for event in events:
        augmented = curator.curate(event)
        if augmented is not None:
            out.append(augmented)
    return out
