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
async def test_connector_exits_clean_when_the_daemon_dies(tmp_path: Path) -> None:
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

        # Simulate daemon crash: force-close all live connections from the hub side.
        # This is the real crash path — the OS closes the daemon's socket FDs,
        # which sends EOF to each connected connector's socket reader.
        for stream in list(hub._conns.values()):
            await stream.aclose()

        with anyio.fail_after(5):                 # connector must exit on socket EOF
            await anyio.to_thread.run_sync(lambda: worker.join(5))
        try:
            assert not worker.is_alive(), "connector thread did not exit after socket EOF"
            assert result["rc"] == 0                  # clean exit → RemoteChannel falls back
        finally:
            os.close(w_fd)
        tg.cancel_scope.cancel()
