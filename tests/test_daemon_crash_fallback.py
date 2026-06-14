from __future__ import annotations

import io
import os
import threading
from pathlib import Path

import anyio
import pytest

from imp.remote.daemon.server import SouthHub
from imp.remote.drivers.daemon_connector import main as connector_main


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_connector_keeps_polling_when_daemon_dies_and_exits_on_stdin_eof(
    tmp_path: Path,
) -> None:
    # The daemon is the gate, but its death is NOT the connector's death: when the
    # daemon dies the connector keeps polling (so it re-attaches if the daemon
    # returns). It exits only when imp tears it down — here, stdin EOF.
    import imp.remote.drivers.daemon_connector as connector

    sock = str(tmp_path / "rc.sock")
    hub = SouthHub(sock, on_event=lambda cid, line: None)
    r_fd, w_fd = os.pipe()
    env = {
        "IMP_REMOTE_CONTROL_SOCK": sock,
        "IMP_REMOTE_CONTROL_CHANGE_ID": "2026-06-13-abc",
        "IMP_REMOTE_CONTROL_TITLE": "T",
    }
    result: dict[str, int] = {}
    stdout = io.StringIO()

    def run_connector() -> None:
        with os.fdopen(r_fd) as stdin:
            result["rc"] = connector_main(env=env, stdin=stdin, stdout=stdout)

    async with anyio.create_task_group() as tg:
        await hub.open()
        tg.start_soon(hub.serve)
        worker = threading.Thread(target=run_connector)
        worker.start()

        with anyio.fail_after(5):                 # wait for connector to register via hello
            while "2026-06-13-abc" not in hub.live_ids():
                await anyio.sleep(0.02)

        # Simulate a daemon that dies for good: drop its socket name so the
        # connector's re-dials fail, then force-close the live connections (the OS
        # closing the daemon's FDs sends EOF to each connector's socket reader).
        os.unlink(sock)
        for stream in list(hub._conns.values()):
            await stream.aclose()

        # The connector must NOT exit just because the daemon died — give it long
        # enough to have re-dialed and chosen to keep polling.
        await anyio.sleep(connector._POLL_GAP + 0.2)
        assert worker.is_alive(), "connector exited on daemon death instead of polling"

        # Now imp tears it down: stdin EOF → the connector exits cleanly.
        os.close(w_fd)
        with anyio.fail_after(5):
            await anyio.to_thread.run_sync(lambda: worker.join(5))
        assert not worker.is_alive(), "connector did not exit on stdin EOF"
        assert result["rc"] == 0
        tg.cancel_scope.cancel()
