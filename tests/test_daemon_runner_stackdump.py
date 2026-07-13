from __future__ import annotations

import functools
import time
from pathlib import Path

import anyio
import pytest

from imp.remote.daemon.runner import pid_path, run_daemon
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeAdapter:
    def on_event(self, event: dict) -> None:
        pass

    def on_response(self, response: object) -> None:
        pass

    def poll_once(self) -> None:
        time.sleep(0.01)


@pytest.mark.anyio
async def test_run_daemon_installs_stack_dumper_beside_pidfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    calls: list[Path] = []
    monkeypatch.setattr(
        "imp.remote.daemon.runner.install_stack_dumper",
        lambda p: calls.append(p))

    async def _noop_signals(*a: object, **k: object) -> None:
        await anyio.sleep_forever()

    monkeypatch.setattr(
        "imp.remote.daemon.runner._await_shutdown_signals", _noop_signals)

    shutdown = anyio.Event()
    async with anyio.create_task_group() as tg:
        tg.start_soon(functools.partial(
            run_daemon, tmp_path, env={},
            make_adapter=(
                lambda env, submit, subscriptions, alert_all, is_known_quest:
                _FakeAdapter()),
            install_signals=True, shutdown=shutdown))
        with anyio.fail_after(5):
            while not pid_path(tmp_path).exists():
                await anyio.sleep(0.02)
        shutdown.set()

    assert calls == [tmp_path / ".imp" / "remote-control.stack-dump.txt"]
