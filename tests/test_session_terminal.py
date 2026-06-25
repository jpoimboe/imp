from __future__ import annotations

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
from imp.cli.terminal_title import GLYPH_WAITING, TerminalTitle
from imp.engine.change import Change
from imp.remote.session.client import SessionClient
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
    server = SessionSocket(sock, on_control=actions.append)
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
                    coord=coord, solicit=_solicit)

            tg.start_soon(_client)
            with anyio.fail_after(5):
                while server.attached_count != 1:
                    await anyio.sleep(0.02)
            # The client installs its SIGQUIT trap before attaching, so
            # attached_count == 1 proves the trap is live — SIG_DFL here would
            # core-dump the test process; in red (no trap), the assert fires first.
            assert signal.getsignal(signal.SIGQUIT) != signal.SIG_DFL
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
    assert _Driver.event_count == 4  # 3 replayed + 1 live gate all flow to driver


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
async def test_title_flips_working_waiting_back_to_working_and_resets(
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
    working = "\x1b]2;✦ imp · term1 · My Change\x07"
    waiting = f"\x1b]2;{GLYPH_WAITING} imp · term1 · My Change\x07"
    reset = "\x1b]2;\x07"
    assert working in out
    assert waiting in out
    assert out.index(waiting) > out.index(working)
    post_gate = out[out.index(waiting) + len(waiting):]
    assert working in post_gate          # flipped back to working after the reply
    assert out.endswith(reset)           # reset last, on teardown


@pytest.mark.anyio
async def test_replayed_awaiting_does_not_flip_to_waiting(
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

    waiting = f"\x1b]2;{GLYPH_WAITING} imp · term1 · My Change\x07"
    assert waiting not in s.getvalue()


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
