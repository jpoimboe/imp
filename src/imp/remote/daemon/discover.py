from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path


def socket_path(control_root: Path) -> Path:
    return control_root / ".imp" / "remote-control.sock"


@dataclass(frozen=True)
class ChangeInfo:
    """One Change as the `list` control verb reports it (spec control surface)."""
    id: str
    title: str
    status: str
    step: str


def list_changes(control_root: Path) -> list[ChangeInfo]:
    """The in-flight Changes the `list` verb reports — those a user can still act on.

    `_iter_change_ids` enumerates every known Change including the journal
    (completed / merged / abandoned), but those are terminal: surfacing them in a
    remote `/list` is just noise. Keep only the in-flight statuses
    (running / new / paused / merge_blocked / draft); `running` and
    `draft` are derived at display time — there is no persisted `errored`."""
    from imp.cli.main import _INFLIGHT, _iter_change_ids, _row_for_id
    out: list[ChangeInfo] = []
    for change_id in _iter_change_ids(control_root):
        row = _row_for_id(control_root, change_id)
        if row is None:
            continue
        change_id_, status_, step_, _last, title_, _created, _step_label = row
        if status_ not in _INFLIGHT:
            continue
        out.append(ChangeInfo(id=change_id_, title=title_, status=status_, step=step_))
    return out


def snapshot(control_root: Path, change_id: str) -> tuple[str, str, str] | None:
    from imp.cli.main import _row_for_id
    row = _row_for_id(control_root, change_id)
    if row is None:
        return None
    _id, status, step, _last, title, _created, _step_label = row
    return (status, step, title)


def recent_narrative(control_root: Path, change_id: str, n: int) -> list[str]:
    from imp.cli.main import _load_events_for_id
    from imp.remote.drivers.telegram import event_body
    events = _load_events_for_id(control_root, change_id)
    if events is None:
        return []
    bodies: list[str] = []
    for event in events:
        if event.get("type") != "llm_response":
            continue
        body = event_body("llm_response", event.get("payload") or {})
        if body.strip():
            bodies.append(body)
    return bodies[-n:] if n > 0 else []


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
