from __future__ import annotations

import asyncio
import io
import json
import os
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.cli.session_terminal import run_terminal_client
from imp.cli.signals import InterruptCoordinator
from imp.cli.terminal_title import GLYPH_BUSY, GLYPH_IDLE, TerminalTitle
from imp.engine.change import Change
from imp.remote.session.client import SessionClient
from imp.remote.session.protocol import encode_attach, encode_input
from imp.remote.session.server import SessionServer
from imp.remote.session.socket_server import SessionSocket
from tests.conftest import _seed_control


class _TTYStream(io.StringIO):
    def isatty(self) -> bool:
        return True


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
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        on_event({"type": "step_started", "payload": {}})
        on_event({"type": "awaiting_user_input",
                   "payload": {"prompt": "your name?"}})
        reply = await user_chat_input_fn("your name?")
        received.append(reply)
        on_event({"type": "user_message", "payload": {"text": reply}})
        return reply

    rendered: list[str] = []
    prompts: list[str] = []

    class _Driver:
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

    async def _solicit(prompt: str) -> str:
        prompts.append(prompt)
        return "imp"

    coord = InterruptCoordinator()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit)

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

    async def _solicit(_prompt: str) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    client_returned = anyio.Event()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit)
                client_returned.set()

            tg.start_soon(_client)
            # Fire the gesture once the forwarder is attached; before attachment
            # signal_pause has no runner and sends nothing, so retry until the
            # interrupt frame lands on the socket.
            with anyio.fail_after(5):
                while not actions:
                    await coord.signal_pause()
                    await anyio.sleep(0.02)
            assert actions == ["interrupt"]
            assert server.attached_count == 1   # gesture did not detach the client
            await server.close()                # daemon ends → socket drops
            with anyio.fail_after(5):
                await client_returned.wait()
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

    async def _solicit(_prompt: str) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    returned = anyio.Event()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit)
                returned.set()

            tg.start_soon(_client)
            with anyio.fail_after(5):
                while server.attached_count != 1:
                    await anyio.sleep(0.02)
            await server.close()              # daemon ends → client returns
            with anyio.fail_after(5):
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
        with anyio.fail_after(15):
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "step_started", "payload": {}})
        # Force-quit tears down the CLIENT here; the run must keep going.
        with anyio.fail_after(15):
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

    async def _solicit(_prompt: str) -> str:
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
                    coord=coord, solicit=_solicit)

            tg.start_soon(_client)
            with anyio.fail_after(15):
                while coord.escalation_event is None or server.attached_count != 1:
                    await anyio.sleep(0.02)
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
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "step_started", "payload": {}})
        # Ctrl-\ tears down the CLIENT here; the run must keep going.
        with anyio.fail_after(5):
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

    async def _solicit(_prompt: str) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    stepped = anyio.Event()

    class _Driver:
        def on_event(self, event: dict[str, Any]) -> None:
            if event.get("type") == "step_started":
                stepped.set()
        def finish(self) -> None: ...

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit)

            tg.start_soon(_client)
            with anyio.fail_after(5):
                while server.attached_count != 1:
                    await anyio.sleep(0.02)
            # The client installs its SIGQUIT trap before attaching, so
            # attached_count == 1 proves the trap is live — SIG_DFL here would
            # core-dump the test process; in red (no trap), the assert fires first.
            assert signal.getsignal(signal.SIGQUIT) != signal.SIG_DFL
            # Fire only after run_fn has emitted step_started: that proves it left
            # its first attach-wait loop and is parked waiting for the detach.
            # Firing on attached_count == 1 alone lets a load-starved run_fn miss
            # the whole attach->detach round and hang in its first loop (the flake).
            with anyio.fail_after(5):
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
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "reply?"}})
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

    async def _solicit(_prompt: str) -> str:
        return next(replies)

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, change_id="c1")

            tg.start_soon(_client)

    assert server.abandoned


@pytest.mark.anyio
async def test_eoferror_at_gate_detaches_cleanly_and_run_survives(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    survived: list[bool] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "reply?"}})
        # The EOFError at the client must detach it; the run must keep going.
        with anyio.fail_after(5):
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

    async def _solicit(_prompt: str) -> str:
        raise EOFError

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    with anyio.fail_after(15):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit)

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
             "payload": {"prompt": f"q{i}"}} for i in range(3)]
    events_path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    server = SessionServer(sock, hibernate=False, events_path=events_path)
    prompts: list[str] = []
    result: dict[str, Any] = {}

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        on_event({"id": "live", "type": "awaiting_user_input",
                  "payload": {"prompt": "live?"}})
        return await user_chat_input_fn("live?")

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(prompt: str) -> str:
        prompts.append(prompt)
        return "answer"

    class _Driver:
        event_count = 0
        def on_event(self, _event: dict[str, Any]) -> None:
            _Driver.event_count += 1
        def finish(self) -> None: ...

    coord = InterruptCoordinator()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()

            async def _serve() -> None:
                result["r"] = await server.serve(run_fn)

            tg.start_soon(_serve)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit)

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
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "reply?"}})
        # /save detaches the client; the run pauses (survives) — no abandon.
        with anyio.fail_after(5):
            while server.attached_count != 0:
                await server.wait_for_attach_change()
        survived.append(True)
        return "done"

    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str) -> str:
        return "/save"

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    leave_state: dict[str, str] = {}
    with anyio.fail_after(15):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, change_id="c1", leave_state=leave_state)

            tg.start_soon(_client)

    assert survived == [True]
    assert leave_state == {"intent": "save"}
    assert not server.abandoned


@pytest.mark.anyio
async def test_abandon_slash_command_records_abandon_intent(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "reply?"}})
        return await user_chat_input_fn("reply?")

    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    replies = iter(["/abandon"])

    async def _solicit(_prompt: str) -> str:
        return next(replies)

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    leave_state: dict[str, str] = {}
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, change_id="c1", leave_state=leave_state)

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
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        on_event({"type": "step_started", "payload": {}})
        on_event({"type": "awaiting_user_input",
                   "payload": {"prompt": "name?"}})
        await user_chat_input_fn("name?")
        # A wait_for_user resume fires NO new step_started; the first live event
        # the resumed agent emits must flip the glyph back to working.
        on_event({"type": "llm_response", "payload": {}})
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

    async def _solicit(prompt: str) -> str:
        return "imp"

    s = _TTYStream()
    title = TerminalTitle(s, "term1", "My Change")
    coord = InterruptCoordinator()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, title=title)

            tg.start_soon(_client)

    out = s.getvalue()
    busy = f"\x1b]2;{GLYPH_BUSY} imp · term1 · My Change\x07"
    idle = f"\x1b]2;{GLYPH_IDLE} imp · term1 · My Change\x07"
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
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        on_event({"type": "awaiting_user_input", "_replay": True,
                   "payload": {"prompt": "old?"}})
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

    async def _solicit(prompt: str) -> str:  # pragma: no cover - no gate fires
        raise AssertionError("a replayed gate must not solicit")

    s = _TTYStream()
    title = TerminalTitle(s, "term1", "My Change")
    coord = InterruptCoordinator()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, title=title)

            tg.start_soon(_client)

    idle = f"\x1b]2;{GLYPH_IDLE} imp · term1 · My Change\x07"
    assert idle not in s.getvalue()


@pytest.mark.anyio
async def test_abandon_records_materialized_false_for_unmaterialized_draft(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="D", change_id="ab1", base_branch="main")
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "reply?"}})
        return await user_chat_input_fn("reply?")

    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "ab1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str) -> str:
        return "/abandon"

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    leave_state: dict[str, str] = {}
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, change_id="ab1", control_root=tmp_path,
                    leave_state=leave_state)

            tg.start_soon(_client)

    assert server.abandoned
    assert leave_state["intent"] == "abandon"
    assert leave_state["materialized"] == ""


@pytest.mark.anyio
async def test_abandon_records_materialized_true_for_materialized_change(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="D", change_id="ab2", base_branch="main")
    meta_path = ch.change_dir / "change.json"
    meta = json.loads(meta_path.read_text())
    meta["materialized"] = True
    meta_path.write_text(json.dumps(meta))

    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "reply?"}})
        return await user_chat_input_fn("reply?")

    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "ab2", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str) -> str:
        return "/abandon"

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    leave_state: dict[str, str] = {}
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, change_id="ab2", control_root=tmp_path,
                    leave_state=leave_state)

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
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "reply?"}})
        # /clear detaches the client; the run pauses (survives) — no abandon.
        with anyio.fail_after(5):
            while server.attached_count != 0:
                await server.wait_for_attach_change()
        survived.append(True)
        return "done"

    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str) -> str:
        return "/clear"

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    leave_state: dict[str, str] = {}
    with anyio.fail_after(15):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, change_id="c1", leave_state=leave_state)

            tg.start_soon(_client)

    assert survived == [True]
    assert leave_state == {"intent": "clear"}
    assert not server.abandoned


@pytest.mark.anyio
async def test_eoferror_leave_sends_archive_frame_and_records_disposition(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="D", change_id="c1", base_branch="main")
    ch.event_log().append(
        agent_id=None, type="user_message",
        payload={"role": "user", "content": "hi"})
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "reply?"}})
        return await user_chat_input_fn("reply?")

    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str) -> str:
        raise EOFError

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    leave_state: dict[str, str] = {}
    with anyio.fail_after(15):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, change_id="c1", control_root=tmp_path,
                    leave_mode="chat", leave_state=leave_state)

            tg.start_soon(_client)

    assert server.archived is True             # daemon received + applied the frame
    assert leave_state["disposition"] == "archive"
    assert not server.abandoned


@pytest.mark.anyio
async def test_natural_completion_sends_no_disposition_frame(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="D", change_id="c2", base_branch="main")
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        return "done"   # completes without a gate; daemon drops the socket

    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, "c2", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    leave_state: dict[str, str] = {}
    with anyio.fail_after(15):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, change_id="c2", control_root=tmp_path,
                    leave_mode="chat", leave_state=leave_state)

            tg.start_soon(_client)

    assert server.archived is False            # no leave gesture → no frame
    assert "disposition" not in leave_state


@pytest.mark.anyio
async def test_ctrl_d_returns_while_daemon_teardown_still_in_progress(
    tmp_path: Path,
) -> None:
    # Primary latency AC: the shell returns well under a second independent of a
    # slow teardown. Run the real daemon (serve_change_session) with a teardown
    # that blocks until released; assert run_terminal_client returns AND the
    # daemon is still inside its (slow) archive when it does.
    import threading

    from imp.remote.session.daemon import serve_change_session
    from imp.remote.session.discover import session_socket_path

    _seed_control(tmp_path)
    change = Change.create_draft(
        tmp_path, title="D", change_id="slow1", base_branch="main")
    change.event_log().append(
        agent_id=None, type="user_message",
        payload={"role": "user", "content": "hi"})
    sock = str(session_socket_path(change.control_root, change.id))

    archive_started = threading.Event()
    release_archive = threading.Event()
    archived: list[str] = []

    def slow_archive(ch: Change) -> None:
        archive_started.set()
        release_archive.wait(5)
        archived.append(ch.id)

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> str:
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "reply?"}})
        return await user_chat_input_fn("reply?")

    coord = InterruptCoordinator()

    async def _attach(on_event: Callable[[dict[str, Any]], None]) -> SessionClient:
        client = SessionClient(sock, change.id, change.title, on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str) -> str:
        raise EOFError

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    leave_state: dict[str, str] = {}
    client_done = anyio.Event()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            async def _serve() -> None:
                await serve_change_session(
                    change, run_fn=fake_run, hibernate=False,
                    archive_left=slow_archive)

            tg.start_soon(_serve)
            with anyio.fail_after(5):
                while not Path(sock).exists():
                    await anyio.sleep(0.02)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, change_id=change.id, control_root=tmp_path,
                    leave_mode="chat", leave_state=leave_state)
                client_done.set()

            tg.start_soon(_client)
            with anyio.fail_after(2):
                await client_done.wait()              # foreground returns promptly
            with anyio.fail_after(2):
                while not archive_started.is_set():   # daemon now in slow teardown
                    await anyio.sleep(0.02)
            assert archived == []                     # foreground did NOT wait for it
            assert leave_state["disposition"] == "archive"
            release_archive.set()                     # let the daemon finish

    assert archived == [change.id]                    # teardown completed in the daemon


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
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "q1"}})
        return await user_chat_input_fn("q1")

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        captured["on_event"] = on_event
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(prompt: str) -> str:
        # The single real gate drives this one solicit. Block here until the
        # scenario has injected the duplicate re-announce frames, so they are
        # provably buffered in the client's gate stream while this solicit is
        # in flight -- the coalescing drain then empties them deterministically,
        # independent of -n auto load timing.
        prompts.append(prompt)
        solicit_entered.set()
        await duplicates_injected.wait()
        return "answer"

    class _Driver:
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
                    coord=coord, solicit=_solicit)

            tg.start_soon(_client)

            # Wait until the real gate has reached the client and entered the
            # first solicit (proof the gate was delivered and the server armed
            # next_input). Only THEN inject the duplicate re-announce frames for
            # the SAME open gate (emit + resume re-announce + attach re-broadcast).
            with anyio.fail_after(30):
                await solicit_entered.wait()
            dup = {"type": "awaiting_user_input", "payload": {"prompt": "q1"}}
            captured["on_event"](dict(dup))
            captured["on_event"](dict(dup))
            captured["on_event"](dict(dup))
            # Release the one solicit; the client coalesces the three buffered
            # duplicates into the single reply for the one open gate.
            duplicates_injected.set()

    assert prompts == ["q1"]         # exactly one solicit despite three frames
    assert result["r"] == "answer"   # exactly one reply reached the gate


@pytest.mark.anyio
async def test_change_renamed_updates_terminal_title(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        on_event({"type": "step_started", "payload": {}})
        on_event({"type": "change_renamed",
                   "payload": {"new_title": "Renamed Change"}})
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

    async def _solicit(prompt: str) -> str:  # pragma: no cover - no gate fires
        raise AssertionError("no gate fired; solicit must not be called")

    s = _TTYStream()
    title = TerminalTitle(s, "term1", "Old Name")
    coord = InterruptCoordinator()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, title=title)

            tg.start_soon(_client)

    renamed = f"\x1b]2;{GLYPH_BUSY} imp · term1 · Renamed Change\x07"
    assert renamed in s.getvalue()


@pytest.mark.anyio
async def test_replayed_change_renamed_does_not_update_title(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        on_event({"type": "change_renamed", "_replay": True,
                   "payload": {"new_title": "Renamed Change"}})
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

    async def _solicit(prompt: str) -> str:  # pragma: no cover - no gate fires
        raise AssertionError("no gate fired; solicit must not be called")

    s = _TTYStream()
    title = TerminalTitle(s, "term1", "Old Name")
    coord = InterruptCoordinator()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, title=title)

            tg.start_soon(_client)

    renamed = f"\x1b]2;{GLYPH_BUSY} imp · term1 · Renamed Change\x07"
    assert renamed not in s.getvalue()


@pytest.mark.anyio
async def test_sigterm_detaches_client_as_a_leave_and_run_survives(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    survived: list[bool] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "step_started", "payload": {}})
        # SIGTERM tears the CLIENT down (a leave); the run keeps going here — the
        # daemon's lease rule, not the client, decides any pause.
        with anyio.fail_after(5):
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

    async def _solicit(_prompt: str) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    stepped = anyio.Event()

    class _Driver:
        def on_event(self, event: dict[str, Any]) -> None:
            if event.get("type") == "step_started":
                stepped.set()
        def finish(self) -> None: ...

    with anyio.fail_after(15):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)
            tg.start_soon(coord.run_signal_receiver)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit)

            tg.start_soon(_client)
            loop = asyncio.get_running_loop()
            # Wait until the client is attached, its escalation hook is wired, AND
            # the coordinator's SIGTERM loop handler is installed — sending the
            # signal before the handler is live would terminate the test process.
            with anyio.fail_after(5):
                while (coord.escalation_event is None
                       or server.attached_count != 1
                       or signal.SIGTERM not in loop._signal_handlers):
                    await anyio.sleep(0.02)
            # ...and until run_fn has emitted step_started, so it is parked in the
            # detach-wait loop and cannot miss the attach->detach round under load.
            with anyio.fail_after(5):
                await stepped.wait()
            os.kill(os.getpid(), signal.SIGTERM)   # leave gesture
            with anyio.fail_after(5):
                while not survived:
                    await anyio.sleep(0.02)
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
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "step_started", "payload": {}})
        # SIGHUP tears the CLIENT down (a leave); the run keeps going here.
        with anyio.fail_after(5):
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

    async def _solicit(_prompt: str) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    stepped = anyio.Event()

    class _Driver:
        def on_event(self, event: dict[str, Any]) -> None:
            if event.get("type") == "step_started":
                stepped.set()
        def finish(self) -> None: ...

    with anyio.fail_after(15):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)
            tg.start_soon(coord.run_signal_receiver)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit)

            tg.start_soon(_client)
            loop = asyncio.get_running_loop()
            with anyio.fail_after(5):
                while (coord.escalation_event is None
                       or server.attached_count != 1
                       or signal.SIGHUP not in loop._signal_handlers):
                    await anyio.sleep(0.02)
            # ...and until run_fn has emitted step_started, so it is parked in the
            # detach-wait loop and cannot miss the attach->detach round under load.
            with anyio.fail_after(5):
                await stepped.wait()
            os.kill(os.getpid(), signal.SIGHUP)   # hangup = leave gesture
            with anyio.fail_after(5):
                while not survived:
                    await anyio.sleep(0.02)
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

    async def _solicit(_prompt: str) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    returned = anyio.Event()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit)
                returned.set()

            tg.start_soon(_client)
            with anyio.fail_after(5):
                while server.attached_count != 1:
                    await anyio.sleep(0.02)
            # A mid-run Ctrl-D leave: stamps leave_reason and drives the escalation.
            await coord.leave_mid_run(reason="ctrl-d")
            with anyio.fail_after(5):
                await returned.wait()
            with anyio.fail_after(5):
                while not actions:
                    await anyio.sleep(0.02)
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

    async def _solicit(_prompt: str) -> str:
        raise AssertionError("no gate fired; solicit must not be called")

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    returned = anyio.Event()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit)
                returned.set()

            tg.start_soon(_client)
            with anyio.fail_after(5):
                while server.attached_count != 1:
                    await anyio.sleep(0.02)
            # Escalation with NO stamped trigger (a disposition-style leave):
            # the choke point must not synthesize an interrupt-leave frame.
            coord.escalation_event.set()
            with anyio.fail_after(5):
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
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
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

    async def _solicit(prompt: str) -> str:  # pragma: no cover - no gate fires
        raise AssertionError("no gate should fire")

    s = _TTYStream()
    title = TerminalTitle(s, "term1", "My Change")
    coord = InterruptCoordinator()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, title=title)

            tg.start_soon(_client)

    hold = "\x1b]2;⛙ imp · term1 · My Change\x07"
    assert hold in s.getvalue()


@pytest.mark.anyio
async def test_replayed_lane_acquired_sets_hold_glyph(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
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

    async def _solicit(prompt: str) -> str:  # pragma: no cover - no gate fires
        raise AssertionError("no gate should fire")

    s = _TTYStream()
    title = TerminalTitle(s, "term1", "My Change")
    coord = InterruptCoordinator()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, title=title)

            tg.start_soon(_client)

    hold = "\x1b]2;⛙ imp · term1 · My Change\x07"
    assert hold in s.getvalue()


@pytest.mark.anyio
async def test_lane_released_clears_hold_glyph(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        on_event({"type": "lane_acquired",
                   "payload": {"base": "main", "step": "wait-for-head",
                               "glyph": "⛙"}})
        on_event({"type": "lane_released",
                   "payload": {"base": "main", "step": "merge"}})
        # Wait for the post-release working re-render to flush before returning,
        # else teardown can race ahead of the client processing the release.
        hold_r = "\x1b]2;⛙ imp · term1 · My Change\x07"
        busy_r = f"\x1b]2;{GLYPH_BUSY} imp · term1 · My Change\x07"
        with anyio.fail_after(5):
            while not (hold_r in s.getvalue() and busy_r in s.getvalue()
                       and s.getvalue().rindex(busy_r) > s.getvalue().index(hold_r)):
                await anyio.sleep(0.02)
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

    async def _solicit(prompt: str) -> str:  # pragma: no cover - no gate fires
        raise AssertionError("no gate should fire")

    s = _TTYStream()
    title = TerminalTitle(s, "term1", "My Change")
    coord = InterruptCoordinator()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, title=title)

            tg.start_soon(_client)

    out = s.getvalue()
    hold = "\x1b]2;⛙ imp · term1 · My Change\x07"
    busy = f"\x1b]2;{GLYPH_BUSY} imp · term1 · My Change\x07"
    assert hold in out
    assert out.rindex(busy) > out.index(hold)


@pytest.mark.anyio
async def test_lane_evicted_clears_hold_glyph_like_released(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        on_event({"type": "lane_acquired",
                   "payload": {"base": "main", "step": "wait-for-head",
                               "glyph": "⛙"}})
        on_event({"type": "lane_evicted",
                   "payload": {"base": "main", "step": "wait-for-head"}})
        # Wait for the post-evict working re-render to flush before returning,
        # else teardown can race ahead of the client processing the eviction.
        hold_r = "\x1b]2;⛙ imp · term1 · My Change\x07"
        busy_r = f"\x1b]2;{GLYPH_BUSY} imp · term1 · My Change\x07"
        with anyio.fail_after(5):
            while not (hold_r in s.getvalue() and busy_r in s.getvalue()
                       and s.getvalue().rindex(busy_r) > s.getvalue().index(hold_r)):
                await anyio.sleep(0.02)
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

    async def _solicit(prompt: str) -> str:  # pragma: no cover - no gate fires
        raise AssertionError("no gate should fire")

    s = _TTYStream()
    title = TerminalTitle(s, "term1", "My Change")
    coord = InterruptCoordinator()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, title=title)

            tg.start_soon(_client)

    out = s.getvalue()
    hold = "\x1b]2;⛙ imp · term1 · My Change\x07"
    busy = f"\x1b]2;{GLYPH_BUSY} imp · term1 · My Change\x07"
    assert hold in out
    assert out.rindex(busy) > out.index(hold)


@pytest.mark.anyio
async def test_replayed_ordinary_event_does_not_flip_to_busy(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        on_event({"type": "awaiting_user_input",
                   "payload": {"prompt": "name?"}})
        await user_chat_input_fn("name?")
        # A replayed ordinary event must NOT flip activity back to working.
        on_event({"type": "llm_response", "_replay": True, "payload": {}})
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

    async def _solicit(prompt: str) -> str:
        return "imp"

    s = _TTYStream()
    title = TerminalTitle(s, "term1", "My Change")
    coord = InterruptCoordinator()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit, title=title)

            tg.start_soon(_client)

    out = s.getvalue()
    idle = f"\x1b]2;{GLYPH_IDLE} imp · term1 · My Change\x07"
    busy = f"\x1b]2;{GLYPH_BUSY} imp · term1 · My Change\x07"
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
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "answer?"}})
        gate_reached.set()
        reply = await user_chat_input_fn("answer?")
        received.append(reply)
        # Mimic the provider: the gate answer is broadcast as a user_message.
        on_event({"type": "user_message", "payload": {"text": reply}})
        return reply

    rendered: list[str] = []
    solicit_cancelled = anyio.Event()

    class _Driver:
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

    async def _solicit_a(_prompt: str) -> str:
        try:
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            solicit_cancelled.set()
            raise
        return ""  # unreachable: this prompt only ends by cancellation

    coord = InterruptCoordinator()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client_a() -> None:
                await run_terminal_client(
                    attach=_attach_a, driver=_Driver(),
                    coord=coord, solicit=_solicit_a)

            tg.start_soon(_client_a)
            await gate_reached.wait()
            # Second raw-socket client answers the open gate.
            raw = await anyio.connect_unix(sock)
            await raw.send((encode_attach("cB", "T", True) + "\n").encode())
            await raw.send((encode_input("remote-answer") + "\n").encode())
            with anyio.fail_after(5):
                while not received:
                    await anyio.sleep(0.02)
            with anyio.fail_after(5):
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
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "g1?"}})
        r1 = await user_chat_input_fn("g1?")
        received.append(r1)
        on_event({"type": "user_message", "payload": {"text": r1}})
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "g2?"}})
        r2 = await user_chat_input_fn("g2?")
        received.append(r2)
        on_event({"type": "user_message", "payload": {"text": r2}})
        return r2

    rendered: list[str] = []

    class _Driver:
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

    async def _solicit(_prompt: str) -> str:
        return next(replies)

    coord = InterruptCoordinator()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit)

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
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "p?"}})
        return await user_chat_input_fn("p?")

    class _Driver:
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

    async def _solicit(prompt: str) -> str:
        at_solicit["should_pause"] = coord.should_pause
        at_solicit["first_gesture_at"] = coord._first_gesture_at
        return "x"

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(),
                    coord=coord, solicit=_solicit)

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
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "name?"}})
        reply = await user_chat_input_fn("name?")
        received.append(reply)
        on_event({"type": "user_message", "payload": {"text": reply}})
        return reply

    echoed: list[str] = []

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str) -> str:
        return "imp"

    coord = InterruptCoordinator()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, change_id="c1", local_echo=echoed.append)

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
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "answer?"}})
        gate_reached.set()
        reply = await user_chat_input_fn("answer?")
        received.append(reply)
        on_event({"type": "user_message", "payload": {"text": reply}})
        return reply

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "cA", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str) -> str:
        await anyio.sleep_forever()
        return ""  # unreachable: cancelled when the gate is answered elsewhere

    coord = InterruptCoordinator()
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, change_id="cA", local_echo=echoed.append)

            tg.start_soon(_client)
            await gate_reached.wait()
            # A second raw client answers the open gate remotely.
            raw = await anyio.connect_unix(sock)
            await raw.send((encode_attach("cB", "T", True) + "\n").encode())
            await raw.send((encode_input("remote-answer") + "\n").encode())
            with anyio.fail_after(5):
                while not received:
                    await anyio.sleep(0.02)
            await raw.aclose()

    # The remote client answered; the local client never sent nor echoed.
    assert received == ["remote-answer"]
    assert echoed == []


@pytest.mark.anyio
@pytest.mark.parametrize("command", ["/save", "/clear"])
async def test_local_echo_not_invoked_on_leave_command(
    tmp_path: Path, command: str,
) -> None:
    # A leave command (/save, /clear) detaches the client and returns BEFORE the
    # local_echo / send_input line, so no optimistic echo fires. (/abandon shares
    # the same pre-send early-return placement and is covered by the next test.)
    sock = str(tmp_path / "s.sock")
    server = SessionServer(sock, hibernate=False)
    echoed: list[str] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "reply?"}})
        # The leave command detaches the client; the run survives — no input frame.
        with anyio.fail_after(5):
            while server.attached_count != 0:
                await server.wait_for_attach_change()
        return "done"

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str) -> str:
        return command

    coord = InterruptCoordinator()
    leave_state: dict[str, str] = {}
    with anyio.fail_after(15):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, change_id="c1", leave_state=leave_state,
                    local_echo=echoed.append)

            tg.start_soon(_client)

    # The leave command detached the client before send_input; no local echo fired.
    assert leave_state["intent"] == command.lstrip("/")
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
        with anyio.fail_after(5):
            while server.attached_count == 0:
                await server.wait_for_attach_change()
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "reply?"}})
        # CONTROL_ABANDON cancels the serve scope before user_chat_input_fn returns.
        return await user_chat_input_fn("reply?")

    coord = InterruptCoordinator()

    async def _attach(
        on_event: Callable[[dict[str, Any]], None],
    ) -> SessionClient:
        client = SessionClient(sock, "c1", "T", on_event=on_event)
        await client.connect()
        return client

    async def _solicit(_prompt: str) -> str:
        return "/abandon"

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _client() -> None:
                await run_terminal_client(
                    attach=_attach, driver=_Driver(), coord=coord,
                    solicit=_solicit, change_id="c1", local_echo=echoed.append)

            tg.start_soon(_client)

    # /abandon detached the client before send_input; no local echo fired.
    assert server.abandoned
    assert echoed == []
