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
    (running / paused / errored / merge_blocked)."""
    from imp.cli.main import _INFLIGHT, _iter_change_ids, _row_for_id
    out: list[ChangeInfo] = []
    for change_id in _iter_change_ids(control_root):
        row = _row_for_id(control_root, change_id)
        if row is None:
            continue
        change_id_, status_, step_, _last, title_, _created = row
        if status_ not in _INFLIGHT:
            continue
        out.append(ChangeInfo(id=change_id_, title=title_, status=status_, step=step_))
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
