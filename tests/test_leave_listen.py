"""A mid-run leave listens briefly for the daemon's verdict before it closes."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import pytest
from anyio.abc import SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream

from imp.cli.session_run import _closing_block
from imp.cli.session_terminal import RunWitness, run_terminal_client
from imp.cli.signals import InterruptCoordinator
from imp.engine.quest import Quest
from imp.paths import project_state_dir
from imp.remote.session.client import SessionClient, StreamEnding
from imp.remote.session.protocol import (
    _MAX_FRAME,
    CONTROL_INTERRUPT_LEAVE,
    SkillRequest,
    encode_event,
)
from imp.remote.session.socket_server import SessionSocket
from tests.async_wait import wait_until

_LISTEN = "imp.cli.session_terminal._LEAVE_LISTEN_SECONDS"
# Long enough that a stop condition which never fires hangs into the backstop.
_FOREVER = 3600.0
_QUEST_ID = "2026-11-08-listen"
_MERGED = {"base": "main", "commit": "edacccfd" + "0" * 32,
           "commits": [{"sha": "4091ab6d" + "1" * 32, "subject": "add RACE.txt"}]}
_PAUSE_HEARD = [
    {"type": "interrupt_requested", "payload": {"source": "ctrl-d", "via": "frame"}},
    {"type": "workflow_paused",
     "payload": {"reason": "interrupt", "interrupt_reason": "ctrl-d"}},
]
_VERDICTS = [
    *_PAUSE_HEARD,
    {"type": "awaiting_user_input", "payload": {"steer_hint": "steer?", "interrupt": True}},
    {"type": "quest_merged", "payload": _MERGED},
    {"type": "workflow_completed", "payload": {"commits": []}},
    {"type": "merge_blocked", "payload": {"reason": "conflict"}},
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _no_confirm(question: str) -> bool:
    raise AssertionError(f"unexpected confirm: {question!r}")


async def _no_solicit(_prompt: str, _interrupt: bool = False) -> str:
    raise AssertionError("no gate fired; solicit must not be called")


class _Driver:
    """Records the type of every event it is asked to render."""

    def __init__(self) -> None:
        self.rendered: list[str] = []

    def on_event(self, event: dict[str, Any]) -> None:
        self.rendered.append(str(event.get("type")))

    def finish(self, **_kw: object) -> None: ...
    def clear_stop_ack(self) -> None: ...
    def print_interrupt_recap(self) -> None: ...


class _DeadSendClient(SessionClient):
    """Its leave frame cannot be written, as when the daemon dies mid-leave."""

    async def send_control(self, action: str, reason: str | None = None) -> None:
        if action == CONTROL_INTERRUPT_LEAVE:
            raise anyio.BrokenResourceError
        await super().send_control(action, reason)


@dataclass
class _Session:
    server: SessionSocket
    coord: InterruptCoordinator
    driver: _Driver
    actions: list[tuple[str, str | None]]
    returned: anyio.Event = field(default_factory=anyio.Event)
    result: list[tuple[StreamEnding, RunWitness]] = field(default_factory=list)

    async def leave_heard(self) -> None:
        await wait_until(lambda: bool(self.actions))

    def outcome(self) -> tuple[StreamEnding, RunWitness]:
        return self.result[0]


@contextlib.asynccontextmanager
async def _session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
    solicit: Callable[[str, bool], Awaitable[str]] = _no_solicit,
    client_cls: type[SessionClient] = SessionClient,
    listen: float | None = _FOREVER,
    signals: bool = False,
) -> AsyncIterator[_Session]:
    """A live client on a scripted daemon; `listen=None` keeps the real deadline.
    On exit, waits for the client to return."""
    if listen is not None:
        monkeypatch.setattr(_LISTEN, listen)
    sock = str(tmp_path / "s.sock")
    actions: list[tuple[str, str | None]] = []
    server = SessionSocket(sock, on_control=lambda a, r: actions.append((a, r)))
    session = _Session(server, InterruptCoordinator(), _Driver(), actions)

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = client_cls(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    with anyio.fail_after(30):   # hang backstop: a missed stop condition lands here
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve)
            if signals:
                tg.start_soon(session.coord.run_signal_receiver)

            async def _client() -> None:
                session.result.append(await run_terminal_client(
                    attach=_attach, driver=session.driver, coord=session.coord,
                    solicit=solicit, confirm=_no_confirm, leave_mode="new"))
                session.returned.set()

            tg.start_soon(_client)
            await wait_until(lambda: server.attached_count == 1)
            yield session
            await session.returned.wait()
            await server.close()
            tg.cancel_scope.cancel()


def _closing(tmp_path: Path, session: _Session) -> int:
    """The real closing block over what the real teardown returned."""
    ending, witness = session.outcome()
    quest = Quest(id=_QUEST_ID, title="T", base_branch="main",
                  control_root=tmp_path, project_root=tmp_path,
                  quest_dir=project_state_dir(tmp_path) / "quests" / _QUEST_ID)
    return _closing_block(quest, witness=witness, ending=ending,
                          coord=session.coord, leave_state={})


def _pause_block(label: str) -> str:
    return (f"paused · {label}\n"
            f"{_QUEST_ID} · T\n"
            f"resume: imp resume {_QUEST_ID}\n"
            "\n")


@pytest.mark.anyio
async def test_a_leave_racing_a_merge_renders_the_merged_block_heard_on_the_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    async with _session(tmp_path, monkeypatch) as s:
        await s.coord.leave_mid_run(reason="sigterm")
        await s.leave_heard()
        # The jada05 shape: the daemon merges after the leave frame went out.
        s.server.broadcast({"type": "step_completed", "payload": {}})
        s.server.broadcast({"type": "quest_merged", "payload": _MERGED})
    _ending, witness = s.outcome()
    terminal = witness.terminal()
    assert terminal is not None and terminal.type == "quest_merged"
    assert "step_completed" not in s.driver.rendered
    assert "quest_merged" not in s.driver.rendered
    rc = _closing(tmp_path, s)
    out = capsys.readouterr().out
    assert rc == 0
    assert out == (f"{_QUEST_ID} · T · merged into main as edacccfd\n"
                   "  4091ab6d  add RACE.txt\n"
                   "\n")
    assert "paused" not in out
    assert "resume:" not in out


@pytest.mark.anyio
@pytest.mark.parametrize("verdict", _VERDICTS, ids=lambda e: e["type"])
async def test_each_verdict_heard_after_the_leave_ends_the_listen_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: dict[str, Any],
) -> None:
    async with _session(tmp_path, monkeypatch) as s:
        await s.coord.leave_mid_run(reason="ctrl-d")
        await s.leave_heard()
        s.server.broadcast(verdict)
    ending, _witness = s.outcome()
    assert s.actions == [(CONTROL_INTERRUPT_LEAVE, "ctrl-d")]
    assert ending is StreamEnding.CUT
    assert verdict["type"] not in s.driver.rendered


@pytest.mark.anyio
@pytest.mark.parametrize("heard", _PAUSE_HEARD, ids=lambda e: e["type"])
async def test_a_leave_that_hears_the_pause_renders_its_pause_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], heard: dict[str, Any],
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    async with _session(tmp_path, monkeypatch) as s:
        await s.coord.leave_mid_run(reason="ctrl-d")
        await s.leave_heard()
        s.server.broadcast(heard)
    rc = _closing(tmp_path, s)
    assert rc == 130
    assert capsys.readouterr().out == _pause_block("Ctrl-D")


@pytest.mark.anyio
async def test_a_leave_the_daemon_never_answers_gives_up_after_two_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _session(tmp_path, monkeypatch, listen=None) as s:
        started = anyio.current_time()
        await s.coord.leave_mid_run(reason="ctrl-d")
        await s.returned.wait()
        elapsed = anyio.current_time() - started
        await s.leave_heard()
    assert s.actions == [(CONTROL_INTERRUPT_LEAVE, "ctrl-d")]
    assert s.outcome()[0] is StreamEnding.CUT
    # The spec's 2 s listen; generous headroom above it for a loaded machine.
    assert 1.9 <= elapsed < 10.0


@pytest.mark.anyio
async def test_a_leave_whose_frame_cannot_be_sent_does_not_listen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _session(tmp_path, monkeypatch, client_cls=_DeadSendClient) as s:
        await s.coord.leave_mid_run(reason="ctrl-d")
    assert s.actions == []
    assert s.outcome()[0] is StreamEnding.CUT


@pytest.mark.anyio
async def test_a_daemon_that_drops_the_connection_ends_the_listen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _session(tmp_path, monkeypatch) as s:
        await s.coord.leave_mid_run(reason="ctrl-d")
        await s.leave_heard()
        await s.server.detach_all()
    assert s.outcome()[0] is StreamEnding.CUT


@pytest.mark.anyio
@pytest.mark.parametrize("when", ["before-the-leave", "replayed"])
async def test_an_interrupt_request_not_answering_the_leave_does_not_end_the_listen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str,
) -> None:
    request = {"type": "interrupt_requested", "payload": {"source": "ctrl-c", "via": "frame"}}
    async with _session(tmp_path, monkeypatch) as s:
        if when == "before-the-leave":
            s.server.broadcast(request)
            await wait_until(lambda: "interrupt_requested" in s.driver.rendered)
        await s.coord.leave_mid_run(reason="ctrl-d")
        await s.leave_heard()
        if when == "replayed":
            s.server.broadcast({**request, "_replay": True})
        await anyio.sleep(0.2)   # give an erroneous stop time to land
        assert not s.returned.is_set()
        s.server.broadcast({"type": "workflow_paused", "payload": {"reason": "interrupt"}})
    assert s.outcome()[1].paused() is not None


@pytest.mark.anyio
@pytest.mark.parametrize("trigger", ["ctrl-d", "sigterm"])
async def test_a_leave_at_an_open_interrupt_prompt_closes_without_listening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], trigger: str,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    solicited = anyio.Event()

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        solicited.set()
        if trigger == "ctrl-d":
            raise EOFError
        await anyio.sleep_forever()
        return ""

    async with _session(tmp_path, monkeypatch, solicit=_solicit, signals=True) as s:
        loop = asyncio.get_running_loop()
        # The SIGTERM handler must be live first, or the signal kills the test process.
        await wait_until(lambda: signal.SIGTERM in loop._signal_handlers)
        # The daemon's interrupt hold prompt: pause_reason is latched, so nothing
        # follows the leave frame.
        s.server.broadcast({"type": "awaiting_user_input",
                            "payload": {"steer_hint": "steer?", "interrupt": True}})
        await solicited.wait()
        if trigger == "sigterm":
            os.kill(os.getpid(), signal.SIGTERM)
        await s.returned.wait()
        await s.leave_heard()
    assert s.actions == [(CONTROL_INTERRUPT_LEAVE, trigger)]
    rc = _closing(tmp_path, s)
    assert rc == 130
    label = {"ctrl-d": "Ctrl-D", "sigterm": "SIGTERM"}[trigger]
    assert capsys.readouterr().out == _pause_block(label)


@pytest.mark.anyio
@pytest.mark.parametrize("answered", ["here", "elsewhere", "skill"])
async def test_an_answered_gate_does_not_stop_a_later_leave_from_listening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answered: str,
) -> None:
    solicited = anyio.Event()
    cancelled = anyio.Event()

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str | SkillRequest:
        solicited.set()
        if answered == "here":
            return "go on"
        if answered == "skill":
            return SkillRequest("review", "x")
        try:
            await anyio.sleep_forever()
        finally:
            cancelled.set()
        return ""

    async with _session(tmp_path, monkeypatch, solicit=_solicit) as s:
        if answered in ("here", "skill"):
            s.server.arm_input()
        s.server.broadcast({"type": "awaiting_user_input",
                            "payload": {"steer_hint": "", "interrupt": False}})
        await solicited.wait()
        if answered == "here":
            assert await s.server.next_input() == "go on"
        elif answered == "skill":
            assert await s.server.next_input() == SkillRequest("review", "x")
        else:
            s.server.broadcast({"type": "user_input_resolved", "payload": {}})
            await cancelled.wait()
        await s.coord.leave_mid_run(reason="ctrl-d")
        await s.leave_heard()
        await anyio.sleep(0.2)   # give an erroneous skip time to land
        assert not s.returned.is_set()
        s.server.broadcast({"type": "workflow_paused", "payload": {"reason": "interrupt"}})
    assert s.actions == [(CONTROL_INTERRUPT_LEAVE, "ctrl-d")]


@pytest.mark.anyio
async def test_an_end_frame_heard_while_listening_returns_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _session(tmp_path, monkeypatch) as s:
        await s.coord.leave_mid_run(reason="ctrl-d")
        await s.leave_heard()
        s.server.enqueue_end()
    ending, witness = s.outcome()
    assert ending is StreamEnding.ENDED
    assert witness.leave() is None


@pytest.mark.anyio
async def test_a_frame_split_across_the_leave_is_not_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_LISTEN, _FOREVER)
    sock = str(tmp_path / "s.sock")
    merged = (encode_event({"type": "quest_merged", "payload": _MERGED}) + "\n").encode()
    head, tail = merged[: len(merged) // 2], merged[len(merged) // 2:]
    coord = InterruptCoordinator()
    driver = _Driver()
    result: list[tuple[StreamEnding, RunWitness]] = []
    returned = anyio.Event()

    async def _daemon(stream: SocketStream) -> None:
        async with stream:
            # The head rides in the same write as a rendered event, so it sits in the
            # client's read buffer when the leave fires.
            step = (encode_event({"type": "step_started", "payload": {}}) + "\n").encode()
            await stream.send(step + head)
            lines = BufferedByteReceiveStream(stream)
            while True:
                frame = json.loads(await lines.receive_until(b"\n", _MAX_FRAME))
                if frame.get("action") == CONTROL_INTERRUPT_LEAVE:
                    break
            await stream.send(tail)
            with contextlib.suppress(anyio.EndOfStream, anyio.BrokenResourceError):
                while True:
                    await stream.receive()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    listener = await anyio.create_unix_listener(sock)
    with anyio.fail_after(30):   # hang backstop: a lost frame never ends the listen
        async with anyio.create_task_group() as tg:
            async def _serve() -> None:
                # anyio's serve() does not close the listener on cancellation; own it here.
                async with listener:
                    await listener.serve(_daemon)

            async def _client() -> None:
                result.append(await run_terminal_client(
                    attach=_attach, driver=driver, coord=coord,
                    solicit=_no_solicit, confirm=_no_confirm, leave_mode="new"))
                returned.set()

            tg.start_soon(_serve)
            tg.start_soon(_client)
            await wait_until(lambda: "step_started" in driver.rendered)
            await coord.leave_mid_run(reason="ctrl-d")
            await returned.wait()
            tg.cancel_scope.cancel()
    terminal = result[0][1].terminal()
    assert terminal is not None and terminal.type == "quest_merged"
    assert "quest_merged" not in driver.rendered
