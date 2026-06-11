# tests/test_remote_session.py
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.cli.main import _maybe_build_channel, _run_change_session
from imp.cli.remote_channel import RemoteChannel
from imp.cli.signals import InterruptCoordinator
from imp.engine.state import State
from tests._remote_fakes import fake_driver_command

# Reuse the on-disk Change + fake-provider scaffolding from the session-loop suite.
from tests.test_session_loop import _register_fake, _seed_single_agent


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _StubStatus:
    def reset(self) -> None: ...
    def show_stop_ack(self, _t: str) -> None: ...


class _StubLiveDriver:
    def on_event(self, _e: dict) -> None: ...
    def finish(self) -> None: ...


def test_maybe_build_channel_none_without_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))  # no user config
    change = _seed_single_agent(tmp_path)
    assert _maybe_build_channel(change) is None


def test_maybe_build_channel_builds_from_config(tmp_path: Path) -> None:
    change = _seed_single_agent(tmp_path)
    (change.control_root / ".imp" / "config.yaml").write_text(
        "providers: [fake]\n"
        "tiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n"
        "remote:\n  driver: telegram\n")
    ch = _maybe_build_channel(change)
    assert isinstance(ch, RemoteChannel)


@pytest.mark.anyio
async def test_phone_only_driver_reply_continues_the_run(tmp_path: Path) -> None:
    """Headless (no TTY) + a configured driver: the run blocks on the driver line
    alone, the agent receives it as its wait_for_user reply, and the run completes
    (spec AC 7 / 12c) — no terminal, no daemon."""
    change = _seed_single_agent(tmp_path)
    captured: list[str] = []

    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            reply = await inv.user_input_fn("")     # the wait_for_user solicit
            captured.append(reply)
            yield {"type": "agent_output", "payload": {"status": "Success"}}

        async def interrupt(self) -> None: ...

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()

    _register_fake(lambda _m: _P())

    coord = InterruptCoordinator()
    coord._esc_reader = object()
    channel = RemoteChannel(
        command=tuple(fake_driver_command(tmp_path, reply="proceed phone")),
        events=("all",), startup_timeout=2.0, reply_timeout=None,
        id=change.id, title=change.title)

    async def _never_local() -> str:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    rc_holder: dict[str, int] = {}

    async def _runner() -> None:
        async with anyio.create_task_group() as tg:
            rc_holder["rc"] = await _run_change_session(
                change, coord=coord, driver=_StubLiveDriver(),
                status=_StubStatus(), esc_reader=None,
                reply_input_fn=_never_local, non_interactive=True, tg=tg,
                channel=channel)

    with anyio.fail_after(20):
        await _runner()

    assert rc_holder["rc"] == 0
    assert captured == ["proceed phone"]
    assert State.load(change.state_dir).status == "completed"


@pytest.mark.anyio
async def test_channel_receives_events_during_run(tmp_path: Path) -> None:
    """The channel is composed onto the live driver's on_event: run events reach
    the driver process as NDJSON during the run."""
    change = _seed_single_agent(tmp_path)
    log = tmp_path / "drv.log"

    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            _ = await inv.user_input_fn("")
            yield {"type": "agent_output", "payload": {"status": "Success"}}

        async def interrupt(self) -> None: ...

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()

    _register_fake(lambda _m: _P())

    coord = InterruptCoordinator()
    coord._esc_reader = object()
    channel = RemoteChannel(
        command=tuple(fake_driver_command(tmp_path, log=log, reply="go")),
        events=("all",), startup_timeout=2.0, reply_timeout=None,
        id=change.id, title=change.title)

    async def _never_local() -> str:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    async def _runner() -> None:
        async with anyio.create_task_group() as tg:
            await _run_change_session(
                change, coord=coord, driver=_StubLiveDriver(),
                status=_StubStatus(), esc_reader=None,
                reply_input_fn=_never_local, non_interactive=True, tg=tg,
                channel=channel)

    with anyio.fail_after(20):
        await _runner()

    assert log.exists() and log.read_text().strip()
    types = {json.loads(ln)["type"] for ln in log.read_text().splitlines()}
    assert "step_started" in types     # run events were framed to the driver
