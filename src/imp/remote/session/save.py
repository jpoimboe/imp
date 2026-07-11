"""Save a (possibly resident) Quest to the Backlog."""
from __future__ import annotations

from pathlib import Path

import anyio

from imp.engine.abandon import save_to_backlog
from imp.engine.quest import Quest
from imp.remote.daemon.directory import session_is_resident
from imp.remote.session.client import SessionClient
from imp.remote.session.discover import session_socket_path
from imp.remote.session.protocol import CONTROL_SAVE


def save_change(control_root: Path, quest_id: str) -> None:
    if session_is_resident(control_root, quest_id):
        _deliver_save_resident(control_root, quest_id)
    else:
        ch = Quest.load(control_root, quest_id)
        if ch.materialized:
            return
        save_to_backlog(ch)


_SAVE_CLOSE_TIMEOUT = 10  # seconds to wait for resident session to close on save


def _deliver_save_resident(control_root: Path, quest_id: str) -> None:
    async def _run() -> None:
        path = str(session_socket_path(control_root, quest_id))
        client = SessionClient(path, quest_id, "", on_event=lambda _e: None)
        await client.connect()
        try:
            await client.send_control(CONTROL_SAVE)
            with anyio.fail_after(_SAVE_CLOSE_TIMEOUT):
                await client.receive_events()   # returns when the session closes
        finally:
            await client.aclose()

    anyio.run(_run)
