from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.agents.transcript import SkillReply
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.session_terminal import run_terminal_client
from imp.cli.signals import InterruptCoordinator
from imp.cli.terminal_title import GLYPH_BUSY, GLYPH_IDLE, TerminalTitle
from imp.cli.terminal_watchdog import WATCHDOG_THREAD_NAME, WatchedStream
from imp.engine.quest import Quest
from imp.remote.session.client import SessionClient, StreamEnding
from imp.remote.session.protocol import (
    _MAX_FRAME,
    SkillRequest,
    encode_attach,
    encode_event,
    encode_input,
)
from imp.remote.session.server import SessionServer
from imp.remote.session.socket_server import SessionSocket
from tests._session_fakes import serve_raw_frames
from tests.async_wait import HANG_BACKSTOP, wait_until
from tests.conftest import _seed_control


class _TTYStream(io.StringIO):
    def isatty(self) -> bool:
        return True


class _NullDriver:
    """Structural stand-in for LiveDriver: the methods run_terminal_client calls on
    every gate, as no-ops. Subclasses override only what they record."""

    def on_event(self, _event: dict[str, Any]) -> None: ...
    def finish(self) -> None: ...
    def clear_stop_ack(self) -> None: ...


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_terminal_client_renders_events_and_answers_one_gate(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    received: list[str] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "step_started", "payload": {}})
        on_event({"type": "awaiting_user_input",
                   "payload": {"steer_hint": "your name?"}})
        reply = await user_chat_input_fn("your name?")
        received.append(reply)
        on_event({"type": "user_message", "payload": {"text": reply}})
        return reply

    rendered: list[str] = []
    prompts: list[str] = []

    class _Driver(_NullDriver):
        def on_event(self, event: dict[str, Any]) -> None:
            rendered.append(event.get("type"))

        def finish(self) -> None:
            rendered.append("__finish__")

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(prompt: str, _interrupt: bool = False) -> str:
        prompts.append(prompt)
        return "imp"

    coord = InterruptCoordinator()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, leave_mode="new")

            tg.start_soon(_client)

    assert received == ["imp"]
    assert prompts == ["your name?"]
    assert rendered == [
        "step_started", "awaiting_user_input", "user_message", "__finish__"]


@pytest.mark.anyio
async def test_terminal_client_forwards_interrupt_gesture_as_a_control_frame(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    actions: list[str] = []
    server = SessionSocket(sock, on_control=lambda a, _r: actions.append(a))
    coord = InterruptCoordinator()

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    client_returned = anyio.Event()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, leave_mode="new")
                client_returned.set()

            tg.start_soon(_client)
            # Fire the gesture once the forwarder is attached; before attachment
            # signal_pause has no runner and sends nothing, so retry until the
            # interrupt frame lands on the socket.
            with anyio.fail_after(30):   # deadlock backstop only
                while not actions:
                    await coord.signal_pause()
                    await anyio.sleep(0.02)
            assert actions == ["interrupt"]
            assert server.attached_count == 1   # gesture did not detach the client
            await server.close()                # daemon ends → socket drops
            with anyio.fail_after(30):   # deadlock backstop only
                await client_returned.wait()
            tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_repeat_ctrl_c_forces_silent_daemon_run_after_delivery_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.signals as signals

    sock = str(tmp_path / "s.sock")
    daemon_coord = InterruptCoordinator()
    server = SessionServer(sock, coordinator=daemon_coord, hibernate=False)
    client_coord = InterruptCoordinator()
    clock = {"now": 100.0}
    client_coord._now = lambda: clock["now"]
    cleanup_started = anyio.Event()
    cleanup_release = anyio.Event()
    ack_release = anyio.Event()
    deadline_started = anyio.Event()
    deadline_release = anyio.Event()
    watchdog_started = anyio.Event()
    watchdog_release = anyio.Event()
    finish_run = anyio.Event()
    force_seen = anyio.Event()
    client_done = anyio.Event()
    force_calls: list[str] = []
    records: list[dict[str, Any]] = []
    seen: list[str] = []

    async def wait_deadline() -> None:
        deadline_started.set()
        await deadline_release.wait()

    async def held_watchdog() -> None:
        watchdog_started.set()
        await watchdog_release.wait()

    async def silent_interrupt() -> None:
        cleanup_started.set()
        with anyio.CancelScope(shield=True):
            await cleanup_release.wait()

    async def force_kill() -> None:
        force_calls.append("step:p/s")
        force_seen.set()

    monkeypatch.setattr(signals, "_wait_interrupt_deadline", wait_deadline)
    monkeypatch.setattr(daemon_coord, "_yield_watchdog", held_watchdog)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        class _BroadcastLog:
            def append(self, *, agent_id: Any, type: str, payload: dict[str, Any]):
                record = {
                    "id": f"i{len(records)}", "agent_id": agent_id,
                    "type": type, "payload": payload,
                }
                records.append(record)
                on_event(record)
                return record

        daemon_coord.set_event_log(_BroadcastLog())
        with daemon_coord.runner_attached(
            silent_interrupt, force_kill=force_kill,
            interrupt_ack=ack_release.wait, agent_id="step:p/s",
        ):
            await finish_run.wait()
        return "done"

    class _Driver:
        def on_event(self, event: dict[str, Any]) -> None:
            seen.append(event["type"])

        def finish(self) -> None:
            ...

    async def attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def solicit(_prompt: str, _interrupt: bool = False) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    async def run_client() -> None:
        await run_terminal_client(
            attach=attach, driver=_Driver(), coord=client_coord,
            solicit=solicit, leave_mode="new")
        client_done.set()

    with anyio.fail_after(120):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)
            tg.start_soon(client_coord.run_signal_receiver)
            tg.start_soon(run_client)
            await wait_until(lambda: server.attached_count == 1)
            await wait_until(lambda: bool(client_coord._runner_stack))
            await wait_until(lambda: client_coord._sig_send is not None)

            client_coord._dispatch_signal(signal.SIGINT)
            await cleanup_started.wait()
            await deadline_started.wait()
            await watchdog_started.wait()
            await wait_until(
                lambda: "interrupt_delivery_started" in seen
                and client_coord._delivery_observed)

            clock["now"] = 100.5
            client_coord._dispatch_signal(signal.SIGINT)
            assert force_calls == []

            clock["now"] = 101.5
            client_coord._dispatch_signal(signal.SIGINT)
            await force_seen.wait()
            await wait_until(lambda: any(
                r["type"] == "interrupt_escalated"
                and r["payload"]["reason"] == "second_gesture"
                for r in records))
            assert force_calls == ["step:p/s"]
            escalations = [
                r["payload"] for r in records
                if r["type"] == "interrupt_escalated"]
            assert escalations == [{
                "reason": "second_gesture", "killed": ["step:p/s"]}]
            assert not deadline_release.is_set()
            assert not watchdog_release.is_set()

            ack_release.set()
            cleanup_release.set()
            deadline_release.set()
            watchdog_release.set()
            finish_run.set()
            await client_done.wait()
            tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_terminal_client_detaches_interrupt_forwarder_on_return(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionSocket(sock)
    coord = InterruptCoordinator()

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    returned = anyio.Event()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, leave_mode="new")
                returned.set()

            tg.start_soon(_client)
            await wait_until(lambda: server.attached_count == 1)
            await server.close()              # daemon ends → client returns
            with anyio.fail_after(30):   # deadlock backstop only
                await returned.wait()
            # The forwarder must be detached: with it still attached this would
            # call send_control on the now-closed client and raise RuntimeError.
            await coord.signal_pause()
            tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_force_quit_tears_down_client_and_run_survives(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    survived: list[bool] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "step_started", "payload": {}})
        # Force-quit tears down the CLIENT here; the run must keep going.
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count != 0:
                await server.wait_for_attach_change()
        survived.append(True)
        return "done"

    coord = InterruptCoordinator()

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    with anyio.fail_after(30):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, leave_mode="new")

            tg.start_soon(_client)
            await wait_until(
                lambda: coord.escalation_event is not None
                and server.attached_count == 1
            )
            coord.escalation_event.set()       # force-quit gesture

    assert survived == [True]


@pytest.mark.anyio
async def test_ctrl_backslash_deliberately_detaches_and_run_survives(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    survived: list[bool] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "step_started", "payload": {}})
        # Ctrl-\ tears down the CLIENT here; the run must keep going.
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count != 0:
                await server.wait_for_attach_change()
        survived.append(True)
        return "done"

    coord = InterruptCoordinator()

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    stepped = anyio.Event()

    class _Driver:
        def on_event(self, event: dict[str, Any]) -> None:
            if event.get("type") == "step_started":
                stepped.set()
        def finish(self) -> None: ...

    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, leave_mode="new")

            tg.start_soon(_client)
            await wait_until(lambda: server.attached_count == 1)
            # The client installs its SIGQUIT trap before attaching, so
            # attached_count == 1 proves the trap is live — SIG_DFL here would
            # core-dump the test process; in red (no trap), the assert fires first.
            assert signal.getsignal(signal.SIGQUIT) != signal.SIG_DFL
            # Fire only after run_fn has emitted step_started: that proves it left
            # its first attach-wait loop and is parked waiting for the detach.
            # Firing on attached_count == 1 alone lets a load-starved run_fn miss
            # the whole attach->detach round and hang in its first loop (the flake).
            with anyio.fail_after(30):   # deadlock backstop only
                await stepped.wait()
            os.kill(os.getpid(), signal.SIGQUIT)   # Ctrl-\ deliberate-detach gesture

    assert survived == [True]


@pytest.mark.anyio
async def test_abandon_slash_command_at_gate_sends_control_abandon(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "reply?"}})
        # CONTROL_ABANDON cancels the serve scope before user_chat_input_fn returns
        return await user_chat_input_fn("reply?")

    coord = InterruptCoordinator()

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    replies = iter(["/abandon"])

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        return next(replies)

    class _Driver(_NullDriver):
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def append_committed_reply(self, _text: str) -> None: ...

    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, quest_id="c1", leave_mode="new")

            tg.start_soon(_client)

    assert server.abandoned


@pytest.mark.anyio
async def test_compact_slash_command_at_gate_forwards_control_and_stays_attached(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server_coord = InterruptCoordinator()
    server = SessionServer(sock, coordinator=server_coord, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "reply?"}})
        await wait_until(lambda: server_coord.pause_reason == "compact")
        server_coord.reset()
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "reply?"}})
        return await user_chat_input_fn("reply?")

    client_coord = InterruptCoordinator()

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    replies = iter(["/compact", "/abandon"])

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        return next(replies)

    echoed: list[str] = []

    class _Driver(_NullDriver):
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def append_committed_reply(self, text: str) -> None:
            echoed.append(text)

    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=client_coord, solicit=_solicit, quest_id="c1", leave_mode="new")

            tg.start_soon(_client)

    assert echoed == ["/compact", "/abandon"]
    assert server.abandoned


@pytest.mark.anyio
async def test_control_command_is_echoed_before_handling(tmp_path: Path) -> None:
    # A control slash command is echoed into the scrollback as a committed,
    # non-reconciling reply node BEFORE it is handled (here /abandon, which then
    # tears the client down).
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "reply?"}})
        return await user_chat_input_fn("reply?")

    coord = InterruptCoordinator()

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        return "/abandon"

    echoed: list[str] = []

    class _Driver(_NullDriver):
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def append_committed_reply(self, text: str) -> None:
            echoed.append(text)

    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, quest_id="c1", leave_mode="new")

            tg.start_soon(_client)

    assert echoed == ["/abandon"]     # echoed exactly once, before teardown
    assert server.abandoned


@pytest.mark.anyio
async def test_eoferror_at_gate_detaches_cleanly_and_run_survives(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    survived: list[bool] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "reply?"}})
        # The EOFError at the client must detach it; the run must keep going.
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count != 0:
                await server.wait_for_attach_change()
        survived.append(True)
        return "done"

    coord = InterruptCoordinator()

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        raise EOFError

    class _Driver(_NullDriver):
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, leave_mode="new")

            tg.start_soon(_client)

    assert survived == [True]


@pytest.mark.anyio
async def test_replayed_gates_do_not_crash_only_live_gate_solicits(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    sock = str(tmp_path / "s.sock")
    # Journal holds three prior (resolved) gates; replay re-sends all three on attach.
    recs = [{"id": f"g{i}", "type": "awaiting_user_input",
             "payload": {"steer_hint": f"q{i}"}} for i in range(3)]
    events_path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    server = SessionServer(sock, hibernate=False, events_path=events_path)
    prompts: list[str] = []
    result: dict[str, Any] = {}

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"id": "live", "type": "awaiting_user_input",
                  "payload": {"steer_hint": "live?"}})
        return await user_chat_input_fn("live?")

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(prompt: str, _interrupt: bool = False) -> str:
        prompts.append(prompt)
        return "answer"

    class _Driver(_NullDriver):
        event_count = 0
        def on_event(self, _event: dict[str, Any]) -> None:
            _Driver.event_count += 1
        def finish(self) -> None: ...

    coord = InterruptCoordinator()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()

            async def _serve() -> None:
                result["r"] = await server.serve(run_fn)

            tg.start_soon(_serve)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, leave_mode="new")

            tg.start_soon(_client)

    assert prompts == ["live?"]      # only the live gate solicited
    assert result["r"] == "answer"   # the reply reached the still-open gate
    assert _Driver.event_count == 5  # 3 replayed gates + replay_complete boundary + 1 live gate


@pytest.mark.anyio
async def test_save_slash_command_at_gate_records_intent_and_detaches(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    survived: list[bool] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "reply?"}})
        # /save detaches the client; the run pauses (survives) — no abandon.
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count != 0:
                await server.wait_for_attach_change()
        survived.append(True)
        return "done"

    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        return "/save"

    class _Driver(_NullDriver):
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def append_committed_reply(self, _text: str) -> None: ...

    leave_state: dict[str, str] = {}
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, quest_id="c1", leave_state=leave_state, leave_mode="new")

            tg.start_soon(_client)

    assert survived == [True]
    assert leave_state == {"intent": "save"}
    assert not server.abandoned


@pytest.mark.anyio
async def test_shelve_slash_command_records_shelve_intent(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    survived: list[bool] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "reply?"}})
        # /shelve detaches the client; the run pauses (survives) — no abandon.
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count != 0:
                await server.wait_for_attach_change()
        survived.append(True)
        return "done"

    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        return "/shelve"

    class _Driver(_NullDriver):
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def append_committed_reply(self, _text: str) -> None: ...

    leave_state: dict[str, str] = {}
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, quest_id="c1", leave_state=leave_state, leave_mode="new")

            tg.start_soon(_client)

    assert survived == [True]
    assert leave_state == {"intent": "shelve"}
    assert not server.abandoned


@pytest.mark.anyio
async def test_abandon_slash_command_records_abandon_intent(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "reply?"}})
        return await user_chat_input_fn("reply?")

    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    replies = iter(["/abandon"])

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        return next(replies)

    class _Driver(_NullDriver):
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def append_committed_reply(self, _text: str) -> None: ...

    leave_state: dict[str, str] = {}
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, quest_id="c1", leave_state=leave_state, leave_mode="new")

            tg.start_soon(_client)

    assert server.abandoned
    assert leave_state == {"intent": "abandon"}


@pytest.mark.anyio
async def test_title_flips_busy_idle_back_to_busy_and_resets(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "step_started", "payload": {}})
        on_event({"type": "awaiting_user_input",
                   "payload": {"steer_hint": "name?"}})
        await user_chat_input_fn("name?")
        # A wait_for_user resume fires NO new step_started; the first live event
        # the resumed agent emits must flip the glyph back to working.
        on_event({"type": "agent_message", "payload": {}})
        return "done"

    class _Driver(_NullDriver):
        def on_event(self, event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(prompt: str, _interrupt: bool = False) -> str:
        return "imp"

    s = _TTYStream()
    title = TerminalTitle(s, "term1", "My Quest")
    coord = InterruptCoordinator()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, title=title, leave_mode="new")

            tg.start_soon(_client)

    out = s.getvalue()
    busy = f"\x1b]2;{GLYPH_BUSY} term1 · My Quest\x07"
    idle = f"\x1b]2;{GLYPH_IDLE} term1 · My Quest\x07"
    reset = "\x1b]2;\x07"
    assert busy in out
    assert idle in out
    assert out.index(idle) > out.index(busy)
    post_gate = out[out.index(idle) + len(idle):]
    assert busy in post_gate          # flipped back to busy after the reply
    assert out.endswith(reset)           # reset last, on teardown


@pytest.mark.anyio
async def test_replayed_awaiting_does_not_flip_to_idle(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "awaiting_user_input", "_replay": True,
                   "payload": {"steer_hint": "old?"}})
        on_event({"type": "step_started", "payload": {}})
        return "done"

    class _Driver:
        def on_event(self, event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(
        prompt: str, _interrupt: bool = False
    ) -> str:  # pragma: no cover - no gate fires
        raise AssertionError("a replayed gate must not solicit")

    s = _TTYStream()
    title = TerminalTitle(s, "term1", "My Quest")
    coord = InterruptCoordinator()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, title=title, leave_mode="new")

            tg.start_soon(_client)

    idle = f"\x1b]2;{GLYPH_IDLE} term1 · My Quest\x07"
    assert idle not in s.getvalue()


@pytest.mark.anyio
async def test_abandon_records_materialized_false_for_unmaterialized_draft(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="D", quest_id="ab1", base_branch="main")
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "reply?"}})
        return await user_chat_input_fn("reply?")

    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "ab1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        return "/abandon"

    class _Driver(_NullDriver):
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def append_committed_reply(self, _text: str) -> None: ...

    leave_state: dict[str, str] = {}
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, quest_id="ab1", control_root=tmp_path,
                    leave_state=leave_state, leave_mode="new")

            tg.start_soon(_client)

    assert server.abandoned
    assert leave_state["intent"] == "abandon"
    assert leave_state["materialized"] == ""


@pytest.mark.anyio
async def test_abandon_records_materialized_true_for_materialized_change(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="D", quest_id="ab2", base_branch="main")
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["materialized"] = True
    meta_path.write_text(json.dumps(meta))

    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "reply?"}})
        return await user_chat_input_fn("reply?")

    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "ab2", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        return "/abandon"

    class _Driver(_NullDriver):
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def append_committed_reply(self, _text: str) -> None: ...

    leave_state: dict[str, str] = {}
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, quest_id="ab2", control_root=tmp_path,
                    leave_state=leave_state, leave_mode="new")

            tg.start_soon(_client)

    assert server.abandoned
    assert leave_state["intent"] == "abandon"
    assert leave_state["materialized"] == "1"


@pytest.mark.anyio
async def test_clear_slash_command_at_gate_records_intent_and_detaches(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    survived: list[bool] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "reply?"}})
        # /clear detaches the client; the run pauses (survives) — no abandon.
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count != 0:
                await server.wait_for_attach_change()
        survived.append(True)
        return "done"

    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        return "/clear"

    class _Driver(_NullDriver):
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def append_committed_reply(self, _text: str) -> None: ...

    leave_state: dict[str, str] = {}
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, quest_id="c1", leave_state=leave_state, leave_mode="new")

            tg.start_soon(_client)

    assert survived == [True]
    assert leave_state == {"intent": "clear"}
    assert not server.abandoned


@pytest.mark.anyio
async def test_eoferror_leave_sends_archive_frame_and_records_disposition(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="D", quest_id="c1", base_branch="main")
    ch.event_log().append(
        agent_id=None, type="user_message",
        payload={"role": "user", "content": "hi"})
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "reply?"}})
        return await user_chat_input_fn("reply?")

    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        raise EOFError

    class _Driver(_NullDriver):
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    leave_state: dict[str, str] = {}
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, quest_id="c1", control_root=tmp_path,
                    leave_mode="chat", leave_state=leave_state)

            tg.start_soon(_client)

    assert server.archived is True             # daemon received + applied the frame
    assert leave_state["disposition"] == "archive"
    assert not server.abandoned


@pytest.mark.anyio
async def test_natural_completion_sends_no_disposition_frame(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="D", quest_id="c2", base_branch="main")
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        return "done"   # completes without a gate; daemon drops the socket

    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "c2", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    leave_state: dict[str, str] = {}
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, quest_id="c2", control_root=tmp_path,
                    leave_mode="chat", leave_state=leave_state)

            tg.start_soon(_client)

    assert server.archived is False            # no leave gesture → no frame
    assert "disposition" not in leave_state


@pytest.mark.anyio
async def test_ctrl_d_returns_while_daemon_teardown_still_in_progress(
    tmp_path: Path,
) -> None:
    # Invariant: the shell (run_terminal_client) returns without blocking on a
    # slow daemon teardown. Run the real daemon (serve_quest_session) with a
    # teardown that blocks until released; assert run_terminal_client returns AND
    # the daemon is still inside its (slow) archive when it does.
    import threading

    from imp.remote.session.daemon import serve_quest_session
    from imp.remote.session.discover import session_socket_path

    _seed_control(tmp_path)
    quest = Quest.create_draft(
        tmp_path, title="D", quest_id="slow1", base_branch="main")
    quest.event_log().append(
        agent_id=None, type="user_message",
        payload={"role": "user", "content": "hi"})
    sock = str(session_socket_path(quest.id))

    archive_started = threading.Event()
    release_archive = threading.Event()
    archived: list[str] = []

    def slow_archive(ch: Quest) -> None:
        archive_started.set()
        release_archive.wait(30)   # safety valve only; the test releases explicitly below
        archived.append(ch.id)

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> str:
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "reply?"}})
        return await user_chat_input_fn("reply?")

    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, quest.id, quest.title, on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        raise EOFError

    class _Driver(_NullDriver):
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    leave_state: dict[str, str] = {}
    client_done = anyio.Event()
    with anyio.fail_after(30):   # whole-scenario deadlock backstop (was a tuned 10s)
        async with anyio.create_task_group() as tg:
            async def _serve() -> None:
                await serve_quest_session(
                    quest, run_fn=fake_run, hibernate=False,
                    archive_left=slow_archive)

            tg.start_soon(_serve)
            await wait_until(lambda: Path(sock).exists())

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, quest_id=quest.id, control_root=tmp_path,
                    leave_mode="chat", leave_state=leave_state)
                client_done.set()

            tg.start_soon(_client)
            # No inner budget: foreground returns on detach, bounded only by the outer backstop.
            await client_done.wait()
            # archive_started is a cross-thread threading.Event set inside the
            # daemon's worker-thread teardown; a poll is the idiomatic bridge (no
            # anyio.Event crosses the thread boundary). Bounded by the outer backstop.
            while not archive_started.is_set():
                await anyio.sleep(0.02)
            assert archived == []                     # foreground did NOT wait for it
            assert leave_state["disposition"] == "archive"
            release_archive.set()                     # let the daemon finish

    assert archived == [quest.id]                    # teardown completed in the daemon


@pytest.mark.anyio
async def test_duplicate_live_gates_coalesce_to_one_solicit_without_wouldblock(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    prompts: list[str] = []
    result: dict[str, Any] = {}
    captured: dict[str, Any] = {}
    solicit_entered = anyio.Event()
    duplicates_injected = anyio.Event()

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        # Liveness backstop only; generous so a CPU-starved -n auto worker
        # cannot lapse it (the coalescing asserts below are the real checks).
        with anyio.fail_after(30):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        # Production emit->arm order: emit the gate, THEN arm next_input by
        # awaiting input. The client cannot receive the broadcast until the
        # server yields inside next_input() -- which is AFTER the receiver is
        # armed -- so a reply can never outrun the arm (no reply-before-arm drop).
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "q1"}})
        return await user_chat_input_fn("q1")

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        captured["on_event"] = on_event
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(prompt: str, _interrupt: bool = False) -> str:
        # The single real gate drives this one solicit. Block here until the
        # scenario has injected the duplicate re-announce frames, so they are
        # provably buffered in the client's gate stream while this solicit is
        # in flight -- the coalescing drain then empties them deterministically,
        # independent of -n auto load timing.
        prompts.append(prompt)
        solicit_entered.set()
        await duplicates_injected.wait()
        return "answer"

    class _Driver(_NullDriver):
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    coord = InterruptCoordinator()
    with anyio.fail_after(60):       # whole-scenario liveness backstop
        async with anyio.create_task_group() as tg:
            await server.open()

            async def _serve() -> None:
                result["r"] = await server.serve(run_fn)

            tg.start_soon(_serve)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, leave_mode="new")

            tg.start_soon(_client)

            # Wait until the real gate has reached the client and entered the
            # first solicit (proof the gate was delivered and the server armed
            # next_input). Only THEN inject the duplicate re-announce frames for
            # the SAME open gate (emit + resume re-announce + attach re-broadcast).
            with anyio.fail_after(30):
                await solicit_entered.wait()
            dup = {"type": "awaiting_user_input", "payload": {"steer_hint": "q1"}}
            captured["on_event"](dict(dup))
            captured["on_event"](dict(dup))
            captured["on_event"](dict(dup))
            # Release the one solicit; the client coalesces the three buffered
            # duplicates into the single reply for the one open gate.
            duplicates_injected.set()

    assert prompts == ["q1"]         # exactly one solicit despite three frames
    assert result["r"] == "answer"   # exactly one reply reached the gate


@pytest.mark.anyio
async def test_quest_renamed_updates_terminal_title(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "step_started", "payload": {}})
        on_event({"type": "quest_renamed",
                   "payload": {"new_title": "Renamed Quest"}})
        await anyio.sleep(0.1)
        return "done"

    class _Driver:
        def on_event(self, event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(
        prompt: str, _interrupt: bool = False
    ) -> str:  # pragma: no cover - no gate fires
        raise AssertionError("no gate fired; solicit must not be called")

    s = _TTYStream()
    title = TerminalTitle(s, "term1", "Old Name")
    coord = InterruptCoordinator()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, title=title, leave_mode="new")

            tg.start_soon(_client)

    renamed = f"\x1b]2;{GLYPH_BUSY} term1 · Renamed Quest\x07"
    assert renamed in s.getvalue()


@pytest.mark.anyio
async def test_replayed_quest_renamed_does_not_update_title(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "quest_renamed", "_replay": True,
                   "payload": {"new_title": "Renamed Quest"}})
        on_event({"type": "step_started", "payload": {}})
        await anyio.sleep(0.1)
        return "done"

    class _Driver:
        def on_event(self, event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(
        prompt: str, _interrupt: bool = False
    ) -> str:  # pragma: no cover - no gate fires
        raise AssertionError("no gate fired; solicit must not be called")

    s = _TTYStream()
    title = TerminalTitle(s, "term1", "Old Name")
    coord = InterruptCoordinator()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, title=title, leave_mode="new")

            tg.start_soon(_client)

    renamed = f"\x1b]2;{GLYPH_BUSY} term1 · Renamed Quest\x07"
    assert renamed not in s.getvalue()


@pytest.mark.anyio
async def test_quest_renamed_invokes_on_title_even_on_replay(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "quest_renamed", "_replay": True,
                   "payload": {"new_title": "First"}})
        on_event({"type": "quest_renamed",
                   "payload": {"new_title": "Second"}})
        await anyio.sleep(0.1)
        return "done"

    class _Driver:
        def on_event(self, event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(
        prompt: str, _interrupt: bool = False
    ) -> str:  # pragma: no cover - no gate fires
        raise AssertionError("no gate fired; solicit must not be called")

    seen: list[str] = []
    coord = InterruptCoordinator()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit,
                    on_title=seen.append, leave_mode="new")

            tg.start_soon(_client)

    assert seen == ["First", "Second"]


@pytest.mark.anyio
async def test_sigterm_detaches_client_as_a_leave_and_run_survives(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    survived: list[bool] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "step_started", "payload": {}})
        # SIGTERM tears the CLIENT down (a leave); the run keeps going here — the
        # daemon's lease rule, not the client, decides any pause.
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count != 0:
                await server.wait_for_attach_change()
        survived.append(True)
        return "done"

    coord = InterruptCoordinator()

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    stepped = anyio.Event()

    class _Driver:
        def on_event(self, event: dict[str, Any]) -> None:
            if event.get("type") == "step_started":
                stepped.set()
        def finish(self) -> None: ...

    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)
            tg.start_soon(coord.run_signal_receiver)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, leave_mode="new")

            tg.start_soon(_client)
            loop = asyncio.get_running_loop()
            # Wait until the client is attached, its escalation hook is wired, AND
            # the coordinator's SIGTERM loop handler is installed — sending the
            # signal before the handler is live would terminate the test process.
            await wait_until(
                lambda: coord.escalation_event is not None
                and server.attached_count == 1
                and signal.SIGTERM in loop._signal_handlers
            )
            # ...and until run_fn has emitted step_started, so it is parked in the
            # detach-wait loop and cannot miss the attach->detach round under load.
            with anyio.fail_after(30):   # deadlock backstop only
                await stepped.wait()
            os.kill(os.getpid(), signal.SIGTERM)   # leave gesture
            await wait_until(lambda: survived)
            tg.cancel_scope.cancel()

    assert survived == [True]


@pytest.mark.anyio
async def test_sighup_detaches_client_as_a_leave_and_run_survives(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    survived: list[bool] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "step_started", "payload": {}})
        # SIGHUP tears the CLIENT down (a leave); the run keeps going here.
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count != 0:
                await server.wait_for_attach_change()
        survived.append(True)
        return "done"

    coord = InterruptCoordinator()

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    stepped = anyio.Event()

    class _Driver:
        def on_event(self, event: dict[str, Any]) -> None:
            if event.get("type") == "step_started":
                stepped.set()
        def finish(self) -> None: ...

    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)
            tg.start_soon(coord.run_signal_receiver)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, leave_mode="new")

            tg.start_soon(_client)
            loop = asyncio.get_running_loop()
            await wait_until(
                lambda: coord.escalation_event is not None
                and server.attached_count == 1
                and signal.SIGHUP in loop._signal_handlers
            )
            # ...and until run_fn has emitted step_started, so it is parked in the
            # detach-wait loop and cannot miss the attach->detach round under load.
            with anyio.fail_after(30):   # deadlock backstop only
                await stepped.wait()
            os.kill(os.getpid(), signal.SIGHUP)   # hangup = leave gesture
            await wait_until(lambda: survived)
            tg.cancel_scope.cancel()

    assert survived == [True]


@pytest.mark.anyio
async def test_deliberate_leave_sends_interrupt_leave_with_trigger(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    actions: list[tuple[str, str | None]] = []
    server = SessionSocket(sock, on_control=lambda a, r: actions.append((a, r)))
    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    returned = anyio.Event()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, leave_mode="new")
                returned.set()

            tg.start_soon(_client)
            await wait_until(lambda: server.attached_count == 1)
            # A mid-run Ctrl-D leave: stamps leave_reason and drives the escalation.
            await coord.leave_mid_run(reason="ctrl-d")
            with anyio.fail_after(30):   # deadlock backstop only
                await returned.wait()
            await wait_until(lambda: actions)
            assert actions == [("interrupt-leave", "ctrl-d")]
            await server.close()
            tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_leave_without_a_stamped_trigger_sends_no_leave_frame(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    actions: list[tuple[str, str | None]] = []
    server = SessionSocket(sock, on_control=lambda a, r: actions.append((a, r)))
    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    returned = anyio.Event()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, leave_mode="new")
                returned.set()

            tg.start_soon(_client)
            await wait_until(lambda: server.attached_count == 1)
            # Escalation with NO stamped trigger (a disposition-style leave):
            # the choke point must not synthesize an interrupt-leave frame.
            coord.escalation_event.set()
            with anyio.fail_after(30):   # deadlock backstop only
                await returned.wait()
            await anyio.sleep(0.05)   # give any erroneous frame time to land
            assert actions == []
            await server.close()
            tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_lane_acquired_sets_hold_glyph(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "lane_acquired",
                   "payload": {"base": "main", "step": "wait-for-head",
                               "glyph": "⛙"}})
        return "done"

    class _Driver:
        def on_event(self, event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(
        prompt: str, _interrupt: bool = False
    ) -> str:  # pragma: no cover - no gate fires
        raise AssertionError("no gate should fire")

    s = _TTYStream()
    title = TerminalTitle(s, "term1", "My Quest")
    coord = InterruptCoordinator()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, title=title, leave_mode="new")

            tg.start_soon(_client)

    hold = "\x1b]2;⛙ term1 · My Quest\x07"
    assert hold in s.getvalue()


@pytest.mark.anyio
async def test_replayed_lane_acquired_sets_hold_glyph(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "lane_acquired", "_replay": True,
                   "payload": {"base": "main", "step": "wait-for-head",
                               "glyph": "⛙"}})
        return "done"

    class _Driver:
        def on_event(self, event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(
        prompt: str, _interrupt: bool = False
    ) -> str:  # pragma: no cover - no gate fires
        raise AssertionError("no gate should fire")

    s = _TTYStream()
    title = TerminalTitle(s, "term1", "My Quest")
    coord = InterruptCoordinator()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, title=title, leave_mode="new")

            tg.start_soon(_client)

    hold = "\x1b]2;⛙ term1 · My Quest\x07"
    assert hold in s.getvalue()


@pytest.mark.anyio
async def test_lane_released_clears_hold_glyph(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "lane_acquired",
                   "payload": {"base": "main", "step": "wait-for-head",
                               "glyph": "⛙"}})
        on_event({"type": "lane_released",
                   "payload": {"base": "main", "step": "merge"}})
        # Wait for the post-release working re-render to flush before returning,
        # else teardown can race ahead of the client processing the release.
        hold_r = "\x1b]2;⛙ term1 · My Quest\x07"
        busy_r = f"\x1b]2;{GLYPH_BUSY} term1 · My Quest\x07"
        await wait_until(
            lambda: hold_r in s.getvalue()
            and busy_r in s.getvalue()
            and s.getvalue().rindex(busy_r) > s.getvalue().index(hold_r)
        )
        return "done"

    class _Driver:
        def on_event(self, event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(
        prompt: str, _interrupt: bool = False
    ) -> str:  # pragma: no cover - no gate fires
        raise AssertionError("no gate should fire")

    s = _TTYStream()
    title = TerminalTitle(s, "term1", "My Quest")
    coord = InterruptCoordinator()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, title=title, leave_mode="new")

            tg.start_soon(_client)

    out = s.getvalue()
    hold = "\x1b]2;⛙ term1 · My Quest\x07"
    busy = f"\x1b]2;{GLYPH_BUSY} term1 · My Quest\x07"
    assert hold in out
    assert out.rindex(busy) > out.index(hold)


@pytest.mark.anyio
async def test_lane_evicted_clears_hold_glyph_like_released(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "lane_acquired",
                   "payload": {"base": "main", "step": "wait-for-head",
                               "glyph": "⛙"}})
        on_event({"type": "lane_evicted",
                   "payload": {"base": "main", "step": "wait-for-head"}})
        # Wait for the post-evict working re-render to flush before returning,
        # else teardown can race ahead of the client processing the eviction.
        hold_r = "\x1b]2;⛙ term1 · My Quest\x07"
        busy_r = f"\x1b]2;{GLYPH_BUSY} term1 · My Quest\x07"
        await wait_until(
            lambda: hold_r in s.getvalue()
            and busy_r in s.getvalue()
            and s.getvalue().rindex(busy_r) > s.getvalue().index(hold_r)
        )
        return "done"

    class _Driver:
        def on_event(self, event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(
        prompt: str, _interrupt: bool = False
    ) -> str:  # pragma: no cover - no gate fires
        raise AssertionError("no gate should fire")

    s = _TTYStream()
    title = TerminalTitle(s, "term1", "My Quest")
    coord = InterruptCoordinator()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, title=title, leave_mode="new")

            tg.start_soon(_client)

    out = s.getvalue()
    hold = "\x1b]2;⛙ term1 · My Quest\x07"
    busy = f"\x1b]2;{GLYPH_BUSY} term1 · My Quest\x07"
    assert hold in out
    assert out.rindex(busy) > out.index(hold)


@pytest.mark.anyio
async def test_replayed_ordinary_event_does_not_flip_to_busy(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "awaiting_user_input",
                   "payload": {"steer_hint": "name?"}})
        await user_chat_input_fn("name?")
        # A replayed ordinary event must NOT flip activity back to working.
        on_event({"type": "agent_message", "_replay": True, "payload": {}})
        return "done"

    class _Driver(_NullDriver):
        def on_event(self, event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(prompt: str, _interrupt: bool = False) -> str:
        return "imp"

    s = _TTYStream()
    title = TerminalTitle(s, "term1", "My Quest")
    coord = InterruptCoordinator()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, title=title, leave_mode="new")

            tg.start_soon(_client)

    out = s.getvalue()
    idle = f"\x1b]2;{GLYPH_IDLE} term1 · My Quest\x07"
    busy = f"\x1b]2;{GLYPH_BUSY} term1 · My Quest\x07"
    assert idle in out
    post_gate = out[out.index(idle) + len(idle):]
    assert busy not in post_gate


@pytest.mark.anyio
async def test_remote_answer_tears_down_local_prompt_without_resending(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    received: list[str] = []
    gate_reached = anyio.Event()

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "answer?"}})
        gate_reached.set()
        reply = await user_chat_input_fn("answer?")
        received.append(reply)
        # Mimic the provider: the gate answer is broadcast as a user_message.
        on_event({"type": "user_message", "payload": {"text": reply}})
        return reply

    rendered: list[str] = []
    solicit_cancelled = anyio.Event()

    class _Driver(_NullDriver):
        def on_event(self, event: dict[str, Any]) -> None:
            rendered.append(event.get("type"))

        def finish(self) -> None:
            rendered.append("__finish__")

    async def _attach_a(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "cA", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit_a(_prompt: str, _interrupt: bool = False) -> str:
        try:
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            solicit_cancelled.set()
            raise
        return ""  # unreachable: this prompt only ends by cancellation

    coord = InterruptCoordinator()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client_a() -> None:
                await run_terminal_client(
                    attach=_attach_a, driver=_Driver(),
                    coord=coord, solicit=_solicit_a, leave_mode="new")

            tg.start_soon(_client_a)
            await gate_reached.wait()
            # Second raw-socket client answers the open gate.
            raw = await anyio.connect_unix(sock)
            await raw.send((encode_attach("cB", "T", True) + "\n").encode())
            await raw.send((encode_input("remote-answer") + "\n").encode())
            await wait_until(lambda: received)
            with anyio.fail_after(30):   # deadlock backstop only
                await solicit_cancelled.wait()
            await raw.aclose()

    # The daemon's gate reply is the raw client's; A never sent an input frame.
    assert received == ["remote-answer"]
    # A's prompt was torn down (its blocking solicit was cancelled).
    assert solicit_cancelled.is_set()
    # A's driver observed the provider's echo but never the control signal.
    assert "user_message" in rendered
    assert "user_input_resolved" not in rendered


@pytest.mark.anyio
async def test_two_sequential_local_answers_no_spurious_self_cancellation(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    received: list[str] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "g1?"}})
        r1 = await user_chat_input_fn("g1?")
        received.append(r1)
        on_event({"type": "user_message", "payload": {"text": r1}})
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "g2?"}})
        r2 = await user_chat_input_fn("g2?")
        received.append(r2)
        on_event({"type": "user_message", "payload": {"text": r2}})
        return r2

    rendered: list[str] = []

    class _Driver(_NullDriver):
        def on_event(self, event: dict[str, Any]) -> None:
            rendered.append(event.get("type"))

        def finish(self) -> None:
            rendered.append("__finish__")

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    replies = iter(["one", "two"])

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        return next(replies)

    coord = InterruptCoordinator()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, leave_mode="new")

            tg.start_soon(_client)

    # Both replies reached the daemon in order: gate 2 was NOT skipped, so
    # gate 1's own user_input_resolved did not spuriously cancel gate 2.
    assert received == ["one", "two"]
    assert "user_input_resolved" not in rendered


@pytest.mark.anyio
async def test_awaiting_user_input_resets_client_gesture_latch(
    tmp_path: Path,
) -> None:
    """When the run yields to the paused prompt (awaiting_user_input), the client
    gesture latch is cleared, so a ctrl-c round in flight ends and the next
    gesture is fresh."""
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    at_solicit: dict[str, Any] = {}

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "p?"}})
        return await user_chat_input_fn("p?")

    class _Driver(_NullDriver):
        def on_event(self, event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    coord = InterruptCoordinator()
    # A ctrl-c round is in flight before the run yields.
    coord._should_pause = True
    coord._first_gesture_at = 100.0

    async def _solicit(prompt: str, _interrupt: bool = False) -> str:
        at_solicit["should_pause"] = coord.should_pause
        at_solicit["first_gesture_at"] = coord._first_gesture_at
        return "x"

    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, leave_mode="new")

            tg.start_soon(_client)

    assert at_solicit["should_pause"] is False        # latch cleared on yield
    assert at_solicit["first_gesture_at"] is None


@pytest.mark.anyio
async def test_local_echo_invoked_with_reply_on_local_send_path(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    received: list[str] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "name?"}})
        reply = await user_chat_input_fn("name?")
        received.append(reply)
        on_event({"type": "user_message", "payload": {"text": reply}})
        return reply

    echoed: list[str] = []

    class _Driver(_NullDriver):
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        return "imp"

    coord = InterruptCoordinator()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, quest_id="c1", local_echo=echoed.append, leave_mode="new")

            tg.start_soon(_client)

    # The reply was forwarded to the daemon AND echoed locally, exactly once.
    assert received == ["imp"]
    assert echoed == ["imp"]


@pytest.mark.anyio
async def test_local_echo_not_invoked_when_answered_elsewhere(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    received: list[str] = []
    gate_reached = anyio.Event()
    echoed: list[str] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "answer?"}})
        gate_reached.set()
        reply = await user_chat_input_fn("answer?")
        received.append(reply)
        on_event({"type": "user_message", "payload": {"text": reply}})
        return reply

    class _Driver(_NullDriver):
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "cA", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        await anyio.sleep_forever()
        return ""  # unreachable: cancelled when the gate is answered elsewhere

    coord = InterruptCoordinator()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, quest_id="cA", local_echo=echoed.append, leave_mode="new")

            tg.start_soon(_client)
            await gate_reached.wait()
            # A second raw client answers the open gate remotely.
            raw = await anyio.connect_unix(sock)
            await raw.send((encode_attach("cB", "T", True) + "\n").encode())
            await raw.send((encode_input("remote-answer") + "\n").encode())
            await wait_until(lambda: received)
            await raw.aclose()

    # The remote client answered; the local client never sent nor echoed.
    assert received == ["remote-answer"]
    assert echoed == []


@pytest.mark.anyio
@pytest.mark.parametrize("command,expected_intent",
                         [("/save", "save"), ("/clear", "clear")])
async def test_local_echo_not_invoked_on_leave_command(
    tmp_path: Path, command: str, expected_intent: str,
) -> None:
    # A leave command (/save, /clear) detaches the client and returns BEFORE the
    # local_echo / send_input line, so no optimistic echo fires. (/abandon shares
    # the same pre-send early-return placement and is covered by the next test.)
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    echoed: list[str] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "reply?"}})
        # The leave command detaches the client; the run survives — no input frame.
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count != 0:
                await server.wait_for_attach_change()
        return "done"

    class _Driver(_NullDriver):
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def append_committed_reply(self, _text: str) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        return command

    coord = InterruptCoordinator()
    leave_state: dict[str, str] = {}
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, quest_id="c1", leave_state=leave_state,
                    local_echo=echoed.append, leave_mode="new")

            tg.start_soon(_client)

    # The leave command detached the client before send_input; no local echo fired.
    assert leave_state["intent"] == expected_intent
    assert echoed == []


@pytest.mark.anyio
async def test_local_echo_not_invoked_on_abandon(
    tmp_path: Path,
) -> None:
    # /abandon cancels the serve scope via controls.abandon() and returns BEFORE
    # the local_echo / send_input line, so no optimistic echo fires. No
    # control_root is needed for the abandon control frame itself.
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    echoed: list[str] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"steer_hint": "reply?"}})
        # CONTROL_ABANDON cancels the serve scope before user_chat_input_fn returns.
        return await user_chat_input_fn("reply?")

    coord = InterruptCoordinator()

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        return "/abandon"

    class _Driver(_NullDriver):
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def append_committed_reply(self, _text: str) -> None: ...

    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, quest_id="c1", local_echo=echoed.append, leave_mode="new")

            tg.start_soon(_client)

    # /abandon detached the client before send_input; no local echo fired.
    assert server.abandoned
    assert echoed == []


@pytest.mark.anyio
async def test_mid_conversation_model_sends_control_not_input(
    tmp_path: Path,
) -> None:
    from imp.remote.session.protocol import CONTROL_SET_MODEL

    sock = str(tmp_path / "s.sock")
    controls_seen: list[tuple[str, str | None]] = []
    inputs_seen: list[str] = []
    server = SessionSocket(
        sock, on_control=lambda a, r: controls_seen.append((a, r)))
    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    replies = iter(["/model anthropic:claude-fable-5", "done"])

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        return next(replies)

    class _Driver(_NullDriver):
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def append_committed_reply(self, _text: str) -> None: ...

    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, leave_mode="new")

            tg.start_soon(_client)

            await wait_until(lambda: server.attached_count == 1)
            # Gate 1: the client answers with /model. It must leave as a
            # set-model control carrying the raw arg, NOT an input frame.
            server.broadcast(
                {"type": "awaiting_user_input", "payload": {"steer_hint": "q1"}})
            await wait_until(
                lambda: (CONTROL_SET_MODEL, "anthropic:claude-fable-5")
                in controls_seen
            )
            # The control did not tear the client down: it stays attached and
            # answers the next gate through the same loop.
            assert server.attached_count == 1
            # Gate 2: broadcast then arm next_input in the same task (the
            # production emit->arm order) so the "done" reply cannot outrun the
            # receiver. This reply IS delivered as an input frame.
            server.broadcast(
                {"type": "awaiting_user_input", "payload": {"steer_hint": "q2"}})
            inputs_seen.append(await server.next_input())
            await server.close()
            tg.cancel_scope.cancel()

    assert (CONTROL_SET_MODEL, "anthropic:claude-fable-5") in controls_seen
    assert inputs_seen == ["done"]
    assert "/model anthropic:claude-fable-5" not in inputs_seen


@pytest.mark.anyio
async def test_save_disposition_leave_sends_reason_on_the_frame(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    # A leave_mode="new" draft with an exchange resolves to CONTROL_SAVE ("save").
    quest = Quest.create_draft(
        tmp_path, title="Draft", quest_id="svcd", base_branch="main")
    quest.event_log().append(
        agent_id=None, type="user_message",
        payload={"role": "user", "content": "hi"})

    sock = str(tmp_path / "s.sock")
    actions: list[tuple[str, str | None]] = []
    server = SessionSocket(sock, on_control=lambda a, r: actions.append((a, r)))
    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, quest.id, "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    returned = anyio.Event()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord, solicit=_solicit,
                    quest_id=quest.id, control_root=tmp_path,
                    leave_state={}, leave_mode="new")
                returned.set()

            tg.start_soon(_client)
            await wait_until(lambda: server.attached_count == 1)
            await coord.leave_mid_run(reason="ctrl-d")
            with anyio.fail_after(30):   # deadlock backstop only
                await returned.wait()
            await wait_until(lambda: actions)
            assert actions == [("save", "ctrl-d")]
            await server.close()
            tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_skill_request_is_routed_and_command_line_echoed(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False, skill_loader=lambda name: "BODY")
    captured: list[object] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> object:
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "reply?"}})
        reply = await user_chat_input_fn("reply?")
        captured.append(reply)
        return reply

    coord = InterruptCoordinator()

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> SkillRequest:
        return SkillRequest("refactor", "src/foo.py")

    echoed: list[str] = []

    class _Driver(_NullDriver):
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def append_committed_reply(self, _text: str) -> None: ...

    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, quest_id="c1",
                    local_echo=echoed.append, leave_mode="new")

            tg.start_soon(_client)

    assert captured == [SkillReply(
        command="/refactor src/foo.py", name="refactor",
        body="BODY\n\nsrc/foo.py\n")]
    assert echoed == ["/refactor src/foo.py"]


@pytest.mark.anyio
async def test_sandbox_off_at_gate_forwards_frame_and_daemon_broadcast_lights_marker(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server_coord = InterruptCoordinator()
    server = SessionServer(sock, hibernate=False, coordinator=server_coord)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(30):   # deadlock backstop only
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input",
                   "payload": {"steer_hint": "reply?"}})
        # Stay alive so the toggle's broadcast reaches the client; the test
        # cancels the task group once the marker lands.
        await anyio.sleep_forever()

    coord = InterruptCoordinator()

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    replies = iter(["/sandbox off"])

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        try:
            return next(replies)
        except StopIteration:
            await anyio.sleep_forever()

    marker: list[bool] = []

    class _Driver(_NullDriver):
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def append_committed_reply(self, _text: str) -> None: ...
        def set_unsandboxed(self, value: bool) -> None:
            marker.append(value)

    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, quest_id="c1", leave_mode="new")

            tg.start_soon(_client)
            # Wait for the daemon broadcast to round-trip and light the marker
            # client-side (not the server-side flip, which happens earlier).
            await wait_until(lambda: marker)
            tg.cancel_scope.cancel()

    # Frame forwarded (server flipped) and the marker lit exactly once — via the
    # broadcast intercept, not a local flip. Re-adding the optimistic flip would
    # make this [True, True]; dropping the intercept would leave it [].
    assert server_coord.unsandboxed is True
    assert marker == [True]


@pytest.mark.anyio
async def test_sandbox_off_resolves_the_parked_gate_without_reprompting(
    tmp_path: Path,
) -> None:
    from imp.agents.transcript import ContinuationNote
    from imp.remote.session.server import SANDBOX_OFF_CONFIRMED, SANDBOX_OFF_NOTE

    sock = str(tmp_path / "s.sock")
    server_coord = InterruptCoordinator()
    server = SessionServer(sock, hibernate=False, coordinator=server_coord)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> Any:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "awaiting_user_input",
                   "payload": {"steer_hint": "reply?"}})
        # Park on the chat gate so _pending_prompt is set to the awaiting gate.
        return await user_chat_input_fn("reply?")

    coord = InterruptCoordinator()

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    # "done" is deliberately never consumed: a regression to the re-prompt would
    # solicit twice, return "done", and fail on both asserts below.
    replies = iter(["/sandbox off", "done"])
    solicit_calls: list[str] = []

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        solicit_calls.append(_prompt)
        try:
            return next(replies)
        except StopIteration:
            await anyio.sleep_forever()

    log: list[str] = []

    class _Driver(_NullDriver):
        def on_event(self, event: dict[str, Any]) -> None:
            if event.get("type") == "script_output":
                log.append("confirm:" + event["payload"]["stdout"])

        def finish(self) -> None: ...

        def append_committed_reply(self, text: str) -> None:
            log.append("caret:" + text)

        def set_unsandboxed(self, value: bool) -> None: ...

    result: dict[str, Any] = {}
    with anyio.fail_after(HANG_BACKSTOP):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()

            async def _run() -> None:
                result["ret"] = await server.serve(run_fn)

            tg.start_soon(_run)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, quest_id="c1", leave_mode="new")

            tg.start_soon(_client)
            await wait_until(lambda: "ret" in result and len(log) == 2)
            tg.cancel_scope.cancel()

    # The run returned on the injected note alone — no send_input, no second reply.
    assert result["ret"] == ContinuationNote(text=SANDBOX_OFF_NOTE)
    assert solicit_calls == ["reply?"]
    # Caret echo is committed before the daemon's confirmation lands.
    assert log == ["caret:/sandbox off", "confirm:" + SANDBOX_OFF_CONFIRMED]
    assert server_coord.unsandboxed is True


@pytest.mark.anyio
async def test_sandbox_on_reprompts_with_confirmation_after_caret(
    tmp_path: Path,
) -> None:
    from imp.remote.session.server import SANDBOX_ON_FROM_OFF

    sock = str(tmp_path / "s.sock")
    server_coord = InterruptCoordinator()
    server = SessionServer(sock, hibernate=False, coordinator=server_coord)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> Any:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "awaiting_user_input",
                   "payload": {"steer_hint": "reply?"}})
        return await user_chat_input_fn("reply?")

    coord = InterruptCoordinator()

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    replies = iter(["/sandbox on", "done"])
    solicit_calls: list[str] = []

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        solicit_calls.append(_prompt)
        try:
            return next(replies)
        except StopIteration:
            await anyio.sleep_forever()

    log: list[str] = []

    class _Driver(_NullDriver):
        def on_event(self, event: dict[str, Any]) -> None:
            if event.get("type") == "script_output":
                log.append("confirm:" + event["payload"]["stdout"])

        def finish(self) -> None: ...

        def append_committed_reply(self, text: str) -> None:
            log.append("caret:" + text)

        def set_unsandboxed(self, value: bool) -> None: ...

    result: dict[str, Any] = {}
    with anyio.fail_after(HANG_BACKSTOP):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()

            async def _run() -> None:
                result["ret"] = await server.serve(run_fn)

            tg.start_soon(_run)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, quest_id="c1", leave_mode="new")

            tg.start_soon(_client)
            await wait_until(lambda: "ret" in result and len(log) == 2)
            tg.cancel_scope.cancel()

    # `on` yields nothing: the gate re-opened and the run only returned once the
    # SECOND solicit's real reply resolved it. An `on` that injected a note would
    # return that note here after a single solicit.
    assert result["ret"] == "done"
    assert solicit_calls == ["reply?", "reply?"]
    assert log == ["caret:/sandbox on", "confirm:" + SANDBOX_ON_FROM_OFF]
    assert server_coord.unsandboxed is False


@pytest.mark.anyio
async def test_client_intercepts_sandbox_marker_without_routing_to_driver(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        # Emitted before the gate over an ordered stream, so the client
        # processes the marker first.
        on_event({"type": "sandbox_marker", "payload": {"unsandboxed": True}})
        on_event({"type": "awaiting_user_input",
                   "payload": {"steer_hint": "go?"}})
        return await user_chat_input_fn("go?")

    flips: list[bool] = []
    seen: list[str] = []

    class _Driver(_NullDriver):
        def on_event(self, event: dict[str, Any]) -> None:
            seen.append(event.get("type"))

        def set_unsandboxed(self, value: bool) -> None:
            flips.append(value)

        def finish(self) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        return "ok"

    coord = InterruptCoordinator()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, leave_mode="new")

            tg.start_soon(_client)

    # The marker drove the driver flag...
    assert flips == [True]
    # ...and was NOT routed into the scrollback tree builder (driver.on_event).
    assert "sandbox_marker" not in seen


@pytest.mark.anyio
async def test_client_intercepts_provider_marker_without_routing_to_driver(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "provider_marker", "payload": {"provider": "codex"}})
        on_event({"type": "provider_marker", "payload": {"provider": None}})
        on_event({"type": "awaiting_user_input",
                   "payload": {"steer_hint": "go?"}})
        return await user_chat_input_fn("go?")

    markers: list[str | None] = []
    seen: list[str] = []

    class _Driver(_NullDriver):
        def on_event(self, event: dict[str, Any]) -> None:
            seen.append(event.get("type"))

        def set_pending_provider(self, name: str | None) -> None:
            markers.append(name)

        def finish(self) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        return "ok"

    coord = InterruptCoordinator()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, leave_mode="new")

            tg.start_soon(_client)

    # The arm and its clear both drove the driver field...
    assert markers == ["codex", None]
    # ...and neither reached the scrollback tree builder, where an unhandled
    # type renders as a FallbackLine.
    assert "provider_marker" not in seen


def test_run_terminal_client_leave_mode_is_required() -> None:
    import inspect
    p = inspect.signature(run_terminal_client).parameters["leave_mode"]
    assert p.default is inspect.Parameter.empty


@pytest.mark.anyio
async def test_interrupt_pause_sends_no_disposition_frame(tmp_path: Path) -> None:
    # A bare mid-run interrupt (SIGINT: no leave gesture, so no leave_reason) that
    # trips escalation via the headless detach must NOT resolve a save/archive
    # disposition — even on a leave_mode="new" draft that would otherwise save. No
    # frame is sent; the daemon stays paused.
    _seed_control(tmp_path)
    quest = Quest.create_draft(
        tmp_path, title="Draft", quest_id="ipnd", base_branch="main")
    quest.event_log().append(
        agent_id=None, type="user_message",
        payload={"role": "user", "content": "hi"})
    sock = str(tmp_path / "s.sock")
    actions: list[tuple[str, str | None]] = []
    server = SessionSocket(sock, on_control=lambda a, r: actions.append((a, r)))
    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, quest.id, "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    returned = anyio.Event()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord, solicit=_solicit,
                    quest_id=quest.id, control_root=tmp_path,
                    leave_state={}, leave_mode="new")
                returned.set()

            tg.start_soon(_client)
            await wait_until(lambda: server.attached_count == 1)
            # Mark the session interrupted WITHOUT a leave gesture (no leave_reason),
            # then trip escalation exactly as the headless detach would.
            coord.request_exit("ctrl-c")
            coord.escalation_event.set()
            with anyio.fail_after(30):   # deadlock backstop only
                await returned.wait()
            await anyio.sleep(0.05)   # give any erroneous frame time to land
            assert actions == []          # no disposition frame; run stays paused
            await server.close()
            tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_interrupt_then_save_intent_still_saves(tmp_path: Path) -> None:
    # An explicit /save after an interrupt still resolves to CONTROL_SAVE — the
    # explicit intent wins even though leave_reason is unset and the run is interrupted.
    _seed_control(tmp_path)
    quest = Quest.create_draft(
        tmp_path, title="Draft", quest_id="itss", base_branch="main")
    quest.event_log().append(
        agent_id=None, type="user_message",
        payload={"role": "user", "content": "hi"})
    sock = str(tmp_path / "s.sock")
    actions: list[tuple[str, str | None]] = []
    server = SessionSocket(sock, on_control=lambda a, r: actions.append((a, r)))
    coord = InterruptCoordinator()
    leave_state: dict[str, str] = {}

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, quest.id, "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    returned = anyio.Event()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord, solicit=_solicit,
                    quest_id=quest.id, control_root=tmp_path,
                    leave_state=leave_state, leave_mode="chat")
                returned.set()

            tg.start_soon(_client)
            await wait_until(lambda: server.attached_count == 1)
            coord.request_exit("ctrl-c")        # interrupted, no leave_reason
            leave_state["intent"] = "save"      # explicit /save
            coord.escalation_event.set()
            with anyio.fail_after(30):   # deadlock backstop only
                await returned.wait()
            await wait_until(lambda: actions)
            assert actions == [("save", None)]   # explicit intent honored despite interrupt
            await server.close()
            tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_terminal_client_threads_interrupt_and_skips_empty_echo(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    got: dict[str, Any] = {}

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "awaiting_user_input",
                   "payload": {"steer_hint": "interrupted · x", "interrupt": True}})
        got["reply"] = await user_chat_input_fn("interrupted · x")
        return got["reply"]

    class _Driver(_NullDriver):
        def on_event(self, event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    seen_interrupt: list[bool] = []

    async def _solicit(_prompt: str, interrupt: bool = False) -> str:
        seen_interrupt.append(interrupt)
        return ""      # empty Enter → continue

    echoed: list[str] = []
    coord = InterruptCoordinator()
    with anyio.fail_after(30):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, local_echo=echoed.append, leave_mode="new")

            tg.start_soon(_client)

    assert seen_interrupt == [True]      # the gate's interrupt flag reached solicit
    assert got["reply"] == ""            # empty reply delivered to the daemon
    assert echoed == []                  # no blank "> " line echoed for an empty reply


@pytest.mark.parametrize(("event_type", "payload"), [
    ("interrupt_delivery_started",
     {"agent_id": "step:p/s", "tier": "cooperative"}),
    ("interrupt_delivered",
     {"agent_id": "step:p/s", "tier": "cooperative", "outcome": "ok"}),
])
@pytest.mark.anyio
async def test_interrupt_record_after_gate_leaves_title_idle(
    tmp_path: Path, event_type: str, payload: dict[str, Any],
) -> None:
    # Delivery can land after the gate on a mid-run Ctrl-C: the record is
    # observational and must not re-mark the run busy while the user is prompted.
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "awaiting_user_input",
                  "payload": {"steer_hint": "steer?", "interrupt": True}})
        on_event({"type": event_type, "payload": payload})
        await user_chat_input_fn("steer?")
        return "done"

    class _Driver(_NullDriver):
        def on_event(self, event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(prompt: str, _interrupt: bool = False) -> str:
        return "imp"

    s = _TTYStream()
    title = TerminalTitle(s, "term1", "My Quest")
    coord = InterruptCoordinator()
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, title=title, leave_mode="new")

            tg.start_soon(_client)

    out = s.getvalue()
    busy = f"\x1b]2;{GLYPH_BUSY} term1 · My Quest\x07"
    idle = f"\x1b]2;{GLYPH_IDLE} term1 · My Quest\x07"
    assert idle in out                            # the gate marked it idle
    post_gate = out[out.index(idle) + len(idle):]
    assert busy not in post_gate                  # the interrupt record did not undo it
    assert out.endswith("\x1b]2;\x07")            # reset last, on teardown


@pytest.mark.anyio
async def test_only_a_live_interrupt_delivery_start_arms_the_force_quit_offer(
    tmp_path: Path,
) -> None:
    """The force-quit offer is gated on a live delivery-start record.
    Replayed records from earlier rounds must not arm it."""
    from imp.cli.signals import _FORCE_FORWARD

    events_path = tmp_path / "events.jsonl"
    # An earlier round's delivery, already on disk: replay re-sends it on attach.
    events_path.write_text(json.dumps(
        {"id": "d0", "type": "interrupt_delivery_started",
         "payload": {"agent_id": "codex", "tier": "cooperative"}}) + "\n")
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False, events_path=events_path)
    deliver = anyio.Event()
    done = anyio.Event()
    seen: list[tuple[str | None, bool]] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        await deliver.wait()
        on_event({"id": "d1", "type": "interrupt_delivery_started",
                  "payload": {"agent_id": "codex", "tier": "cooperative"}})
        await done.wait()
        return "ok"

    class _Driver:
        def on_event(self, event: dict[str, Any]) -> None:
            seen.append((event.get("type"), bool(event.get("_replay"))))
        def finish(self) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    coord = InterruptCoordinator()
    t = {"now": 100.0}
    coord._now = lambda: t["now"]
    acks: list[str] = []
    coord.set_ack_hook(acks.append)
    sent: list = []

    class _Send:
        def send_nowait(self, item):  # type: ignore[no-untyped-def]
            sent.append(item)

    coord._sig_send = _Send()

    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, leave_mode="new")

            tg.start_soon(_client)
            await wait_until(lambda: ("replay_complete", False) in seen)
            assert ("interrupt_delivery_started", True) in seen   # the replayed copy arrived

            # A stop is unwinding; only a replayed delivery start has arrived.
            coord._should_pause = True
            coord._first_gesture_at = 100.0
            t["now"] = 101.5
            coord._dispatch_signal(signal.SIGINT)
            assert sent == []                    # nothing in flight → no FORCE frame
            assert acks == ["stopping…"]         # ...and no force-quit promise

            deliver.set()
            await wait_until(lambda: ("interrupt_delivery_started", False) in seen)

            t["now"] = 102.5
            coord._dispatch_signal(signal.SIGINT)
            assert sent == [_FORCE_FORWARD]
            assert acks == [
                "stopping…", "stopping… (interrupt again to force-quit)"]

            done.set()


@pytest.mark.anyio
async def test_terminal_client_reports_cut_ending_on_oversized_frame(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    coord = InterruptCoordinator()
    rendered: list[str] = []
    good = encode_event({"type": "step_started", "payload": {}})

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    class _Driver:
        def on_event(self, event: dict[str, Any]) -> None:
            rendered.append(event["type"])

        def finish(self) -> None: ...

    with anyio.fail_after(HANG_BACKSTOP):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await serve_raw_frames(
                sock, good.encode("utf-8") + b"\n" + b"x" * (_MAX_FRAME + 1), tg)
            ending = await run_terminal_client(
                attach=_attach, driver=_Driver(),
                coord=coord, solicit=_solicit, leave_mode="new")
            tg.cancel_scope.cancel()

    assert ending is StreamEnding.CUT
    assert rendered == ["step_started"]   # the pre-fault frame still rendered


@pytest.mark.anyio
async def test_terminal_client_reports_cut_when_a_leave_cancels_the_loop(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionSocket(sock)
    coord = InterruptCoordinator()
    endings: list[StreamEnding] = []

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    with anyio.fail_after(HANG_BACKSTOP):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve)

            async def _client() -> None:
                endings.append(await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, leave_mode="new"))

            tg.start_soon(_client)
            await wait_until(
                lambda: coord.escalation_event is not None
                and server.attached_count == 1
            )
            coord.escalation_event.set()       # deliberate leave: cancels the receive loop
            await wait_until(lambda: endings)
            await server.close()
            tg.cancel_scope.cancel()

    # The loop was cancelled and never returned an ending: it observed no end frame,
    # so CUT is the honest value; session_run's rule 1 keeps the leave rendering.
    assert endings == [StreamEnding.CUT]


@pytest.mark.anyio
async def test_no_watchdog_thread_outlives_a_normal_leased_attach(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    coord = InterruptCoordinator()
    watched = WatchedStream(_TTYStream())
    before = {t for t in threading.enumerate() if t.name == WATCHDOG_THREAD_NAME}
    spawned: set[threading.Thread] = set()
    good = encode_event({"type": "step_started", "payload": {}})

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    rendered: list[str] = []

    class _Driver:
        def on_event(self, event: dict[str, Any]) -> None:
            rendered.append(event["type"])

        def finish(self) -> None:
            # Runs inside run_terminal_client's finally, before the watchdog is
            # joined — so this is where an armed poller is still observable.
            spawned.update(
                {t for t in threading.enumerate()
                 if t.name == WATCHDOG_THREAD_NAME} - before)

    with anyio.fail_after(HANG_BACKSTOP):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            # One frame, then the peer closes with no end frame: receive_events ends CUT.
            await serve_raw_frames(sock, good.encode("utf-8") + b"\n", tg)
            ending = await run_terminal_client(
                attach=_attach, driver=_Driver(), coord=coord,
                solicit=_solicit, leave_mode="new", stream=watched)
            tg.cancel_scope.cancel()

    assert ending is StreamEnding.CUT
    assert rendered == ["step_started"]
    assert len(spawned) == 1              # it really was armed during the attach
    assert not any(t.is_alive() for t in spawned)
    assert watched.on_terminal_gone is None


@pytest.mark.anyio
async def test_provider_switch_reprompts_and_reaches_the_daemon(
    tmp_path: Path,
) -> None:
    # AC 21: a non-parking control typed at a solicit would otherwise leave the
    # client blocked on gate_recv for a prompt frame that never arrives.
    sock = str(tmp_path / "s.sock")
    pinned: list[str] = []
    server = SessionServer(sock, hibernate=False,
                           coordinator=InterruptCoordinator(),
                           set_provider=pinned.append)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "awaiting_user_input",
                   "payload": {"steer_hint": "reply?"}})
        return await user_chat_input_fn("reply?")

    coord = InterruptCoordinator()

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    replies = iter(["/provider codex", "done"])
    solicit_calls: list[str] = []

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        solicit_calls.append(_prompt)
        try:
            return next(replies)
        except StopIteration:
            await anyio.sleep_forever()

    log: list[str] = []
    markers: list[str | None] = []

    class _Driver(_NullDriver):
        def on_event(self, event: dict[str, Any]) -> None:
            log.append("event:" + str(event.get("type")))

        def finish(self) -> None: ...

        def append_committed_reply(self, text: str) -> None:
            log.append("caret:" + text)

        def set_pending_provider(self, name: str | None) -> None:
            markers.append(name)

    result: dict[str, Any] = {}
    with anyio.fail_after(30):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()

            async def _run() -> None:
                result["ret"] = await server.serve(run_fn)

            tg.start_soon(_run)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, quest_id="c1",
                    leave_mode="new")

            tg.start_soon(_client)
            await wait_until(lambda: "ret" in result)
            tg.cancel_scope.cancel()

    assert result["ret"] == "done"
    assert solicit_calls == ["reply?", "reply?"]   # the prompt re-opened
    assert pinned == ["codex"]
    assert "caret:/provider codex" in log
    # The marker drove the driver field and was NOT routed into the tree builder.
    assert markers == ["codex"]
    assert "event:provider_marker" not in log


def _tty_pair() -> tuple[LiveStatus, LiveDriver]:
    out = _TTYStream()
    status = LiveStatus(out)
    status._width_fn = lambda: 80
    return status, LiveDriver(stream=out, color=False, width=80, status=status)


def _opening_events() -> list[dict[str, Any]]:
    return [
        {"id": "1", "agent_id": None, "type": "quest_created",
         "payload": {"title": "T", "quest_id": 1}},
        {"id": "2", "agent_id": None, "type": "step_started",
         "payload": {"step": "planner", "kind": "agent", "phase": "plan",
                     "model": "m", "provider": "anthropic"}},
    ]


@pytest.mark.anyio
async def test_stop_ack_ends_when_a_gate_is_answered_without_a_prompt(
    tmp_path: Path,
) -> None:
    """The sticky `stopping…` ack belongs to one gesture round, and that round is over
    once the gate it produced has been answered. A latch that returns without opening a
    prompt never reaches LiveStatus.suspend() — the only in-run clearer — so answering
    the gate has to end the ack itself."""
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    status, driver = _tty_pair()
    acks: list[str | None] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        for event in _opening_events():
            on_event(event)
        on_event({"id": "3", "agent_id": None, "type": "awaiting_user_input",
                  "payload": {"steer_hint": "", "interrupt": False}})
        reply = await user_chat_input_fn("")
        # _answer_gates clears the ack BEFORE it sends the input frame, so the
        # reply arriving here orders strictly after the clear: no poll needed.
        acks.append(status._stop_ack)
        return reply

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    coord = InterruptCoordinator()
    coord.set_ack_hook(driver.show_stop_ack)

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        # The ctrl-c round that produced this gate; from here the ack IS the footer.
        await coord.signal_pause(reason="ctrl-c")
        assert status._stop_ack == "stopping…"
        return "latched"

    with anyio.fail_after(HANG_BACKSTOP):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=driver, coord=coord,
                    solicit=_solicit, leave_mode="new")

            tg.start_soon(_client)

    assert acks == [None]


@pytest.mark.anyio
async def test_stop_ack_ends_when_the_gate_is_answered_elsewhere(
    tmp_path: Path,
) -> None:
    """Answered-elsewhere skips the prompt too: _watch_resolved cancels the local
    solicit, so the ack has to die on the cancelled path as well as the returning one."""
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    status, driver = _tty_pair()
    received: list[str] = []
    gate_reached = anyio.Event()
    released = anyio.Event()

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        for event in _opening_events():
            on_event(event)
        on_event({"id": "3", "agent_id": None, "type": "awaiting_user_input",
                  "payload": {"steer_hint": "answer?", "interrupt": False}})
        gate_reached.set()
        reply = await user_chat_input_fn("answer?")
        received.append(reply)
        await released.wait()   # hold the run open while the client is inspected
        return reply

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "cA", "T", on_event=on_event)
        await client.connect()
        return client

    coord = InterruptCoordinator()
    coord.set_ack_hook(driver.show_stop_ack)

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        await coord.signal_pause(reason="ctrl-c")
        await anyio.sleep_forever()
        return ""   # unreachable: cancelled when the gate is answered elsewhere

    with anyio.fail_after(HANG_BACKSTOP):   # deadlock backstop only
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=driver, coord=coord,
                    solicit=_solicit, leave_mode="new")

            tg.start_soon(_client)
            await gate_reached.wait()
            await wait_until(lambda: status._stop_ack == "stopping…")
            # A second raw client answers the open gate remotely.
            raw = await anyio.connect_unix(sock)
            await raw.send((encode_attach("cB", "T", True) + "\n").encode())
            await raw.send((encode_input("remote-answer") + "\n").encode())
            await wait_until(lambda: received)
            # The local solicit was cancelled with no prompt ever drawn; the ack must
            # die with it, while the run is still live (finish() is far too late).
            await wait_until(lambda: status._stop_ack is None)
            released.set()
            await raw.aclose()

    assert received == ["remote-answer"]
