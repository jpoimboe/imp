from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from imp.engine.executor import RunResult
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.remote.session.client import SessionClient
from imp.remote.session.daemon import _is_interrupt_pause, serve_quest_session
from imp.remote.session.discover import session_socket_path
from imp.remote.session.protocol import (
    CONTROL_ABANDON,
    CONTROL_ARCHIVE,
    CONTROL_DISCARD,
    CONTROL_SAVE,
)
from tests.async_wait import wait_until


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _stub_stack_dumper(monkeypatch: pytest.MonkeyPatch) -> None:
    import imp.remote.session.daemon as daemon
    monkeypatch.setattr(daemon, "install_stack_dumper", lambda p: None)


def _make_change(tmp_path: Path, quest_id: str) -> Quest:
    quest = Quest(
        id=quest_id,
        title="T",
        base_branch="main",
        control_root=tmp_path,
        project_root=tmp_path,
        quest_dir=tmp_path / ".imp" / "change",
    )
    State(id=quest_id, step=None, step_attempt=0, status="paused").save(
        quest.state_dir)
    return quest


async def _wait_for_socket(sock: str) -> None:
    with anyio.fail_after(30):  # deadlock backstop only, never a tuned window
        while True:
            try:
                stream = await anyio.connect_unix(sock)
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                await anyio.sleep(0.02)
                continue
            await stream.aclose()
            return


@pytest.mark.anyio
async def test_wait_for_socket_returns_when_listener_accepting(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "ready.sock")
    listener = await anyio.create_unix_listener(sock)
    async with listener:
        with anyio.fail_after(5):
            await _wait_for_socket(sock)


@pytest.mark.anyio
async def test_wait_for_socket_waits_when_file_exists_but_no_listener(
    tmp_path: Path,
) -> None:
    sock = str(tmp_path / "bare.sock")
    Path(sock).write_bytes(b"")  # file exists, but nothing is listening
    with pytest.raises(TimeoutError):
        with anyio.fail_after(0.3):
            await _wait_for_socket(sock)


def _seed_step(quest: Quest, step: str) -> None:
    st = State.load(quest.state_dir)
    st.step = step
    st.save(quest.state_dir)


@pytest.mark.anyio
async def test_serves_on_per_change_socket_and_bridges_run_quest_seams(
    tmp_path: Path,
) -> None:
    quest = _make_change(tmp_path, "2026-06-15-abc")
    sock = str(session_socket_path(quest.control_root, quest.id))

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> str:
        # Emit the prompt before soliciting (as run_quest does): the client's readiness signal.
        on_event({"type": "awaiting_user_input",
                  "payload": {"steer_hint": "go", "interrupt": False}})
        reply = await user_chat_input_fn("")
        on_event({"type": "agent_result", "payload": {"reply": reply}})
        return reply

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}
        run_done = anyio.Event()

        async def _serve() -> None:
            result["r"] = await serve_quest_session(
                quest, run_fn=fake_run, hibernate=False)
            run_done.set()

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        received: list[dict[str, Any]] = []
        gate_seen = anyio.Event()
        output_seen = anyio.Event()

        def _on_event(e: dict[str, Any]) -> None:
            received.append(e)
            if e.get("type") == "awaiting_user_input":
                gate_seen.set()
            if e.get("type") == "agent_result":
                output_seen.set()

        client = SessionClient(sock, quest.id, quest.title, on_event=_on_event)
        await client.connect()
        tg.start_soon(client.receive_events)
        with anyio.fail_after(30):   # deadlock backstop only
            await gate_seen.wait()   # the gate is armed; the reply cannot be lost
        await client.send_input("ship it")
        with anyio.fail_after(30):   # deadlock backstop only; starts after send
            await run_done.wait()
        assert result["r"] == "ship it"
        with anyio.fail_after(30):   # deadlock backstop only
            await output_seen.wait()
        assert received[0] == {"type": "replay_complete"}  # boundary leads the attach stream
        assert [e for e in received
                if e["type"] not in (
                    "user_input_resolved", "replay_complete",
                    "awaiting_user_input")] == [
            {"type": "agent_result", "payload": {"reply": "ship it"}}]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_client_attached_at_completion_receives_final_agent_output(
    tmp_path: Path,
) -> None:
    quest = _make_change(tmp_path, "2026-06-15-flush")
    sock = str(session_socket_path(quest.control_root, quest.id))
    client_ready = anyio.Event()

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> str:
        # Emit the final event as the very last action before returning, so the
        # pump must flush it before the teardown cancel or it is lost.
        await client_ready.wait()
        on_event({"type": "agent_output", "payload": {"reply": "final"}})
        return "done"

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}
        run_done = anyio.Event()

        async def _serve() -> None:
            result["r"] = await serve_quest_session(
                quest, run_fn=fake_run, hibernate=False)
            run_done.set()

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        received: list[dict[str, Any]] = []
        attached = anyio.Event()
        output_seen = anyio.Event()

        def _on_event(e: dict[str, Any]) -> None:
            received.append(e)
            if e.get("type") == "replay_complete":
                attached.set()
            if e.get("type") == "agent_output":
                output_seen.set()

        client = SessionClient(sock, quest.id, quest.title, on_event=_on_event)
        await client.connect()
        tg.start_soon(client.receive_events)
        with anyio.fail_after(30):   # deadlock backstop only
            await attached.wait()    # client fully attached (got replay boundary)
        client_ready.set()           # now fake_run may emit its final event + return
        with anyio.fail_after(30):   # deadlock backstop only
            await output_seen.wait()
        assert {"type": "agent_output", "payload": {"reply": "final"}} in received
        with anyio.fail_after(30):   # deadlock backstop only
            await run_done.wait()
        assert result["r"] == "done"
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_zero_client_gate_hibernates_by_default(tmp_path: Path) -> None:
    from imp.engine.awaiting_user_input import AwaitingUserInput

    quest = _make_change(tmp_path, "2026-06-15-def")

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> str:
        try:
            await user_chat_input_fn("")
        except AwaitingUserInput:
            return "hibernated"
        return "resident"

    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(
            quest, run_fn=fake_run, first_attach_timeout=0.1)
    assert result == "hibernated"


@pytest.mark.anyio
async def test_lock_is_held_during_run_and_released_after(tmp_path: Path) -> None:
    quest = _make_change(tmp_path, "2026-06-15-ghi")
    lock_path = quest.quest_dir / "lock"
    release = anyio.Event()

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> str:
        await release.wait()
        return "done"

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_quest_session(
                quest, run_fn=fake_run, hibernate=False)

        tg.start_soon(_serve)
        await anyio.sleep(0.05)  # let serve acquire the lock and enter run_fn
        assert lock_path.exists()  # ProjectLock writes the holder-pid file
        release.set()
        await wait_until(lambda: "r" in result)
        assert result["r"] == "done"
        assert not lock_path.exists()  # released on serve() return
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_interrupt_control_pauses_the_shared_coordinator(
    tmp_path: Path,
) -> None:
    quest = _make_change(tmp_path, "2026-06-15-jkl")
    sock = str(session_socket_path(quest.control_root, quest.id))

    run_returned = anyio.Event()

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> str:
        # Await the real interrupt-delivery seam (_deliver_interrupt), not a should_pause poll.
        interrupt_delivered = anyio.Event()

        async def _interrupt() -> None:
            interrupt_delivered.set()

        coordinator.attach_runner(_interrupt)
        await interrupt_delivered.wait()
        return "paused"

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_quest_session(
                quest, run_fn=fake_run, hibernate=False)
            run_returned.set()

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        client = SessionClient(sock, quest.id, quest.title,
                               on_event=lambda _e: None)
        await client.connect()
        await client.send_control("interrupt")
        with anyio.fail_after(30):   # deadlock backstop only; starts after send
            await run_returned.wait()
        assert result["r"] == "paused"
        await client.aclose()
        tg.cancel_scope.cancel()


def test_main_loads_named_change_and_serves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import imp.remote.session.daemon as daemon

    loaded = SimpleNamespace(quest_dir=tmp_path)
    calls: dict[str, Any] = {}

    def fake_load(control_root: Path, quest_id: str) -> object:
        calls["load"] = (control_root, quest_id)
        return loaded

    def fake_run(fn: Any, arg: Any) -> None:
        calls["run"] = (fn, arg)

    monkeypatch.setattr(daemon.Quest, "load", staticmethod(fake_load))
    monkeypatch.setattr(daemon.anyio, "run", fake_run)

    rc = daemon.main(
        ["--control-root", str(tmp_path), "--quest-id", "2026-06-15-abc"])

    assert rc == 0
    assert calls["load"] == (tmp_path, "2026-06-15-abc")
    assert calls["run"] == (daemon.serve_quest_session, loaded)


@pytest.mark.anyio
async def test_resident_abandon_runs_teardown(tmp_path: Path) -> None:
    quest = _make_change(tmp_path, "2026-06-15-aba")
    sock = str(session_socket_path(quest.control_root, quest.id))
    torn: list[Quest] = []

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> str:
        await anyio.sleep_forever()   # only an abandon-cancel ends this run
        return "x"

    async with anyio.create_task_group() as tg:
        async def _serve() -> None:
            await serve_quest_session(
                quest, run_fn=fake_run, hibernate=False,
                tear_down=lambda ch: torn.append(ch))

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        client = SessionClient(sock, quest.id, quest.title,
                               on_event=lambda _e: None)
        await client.connect()
        await client.send_control(CONTROL_ABANDON)
        await wait_until(lambda: torn)
        assert torn == [quest]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_interrupt_resume_prompt_not_duplicated_on_existing_client(
    tmp_path: Path,
) -> None:
    quest = _make_change(tmp_path, "2026-06-15-dup")
    sock = str(session_socket_path(quest.control_root, quest.id))
    calls = {"n": 0}

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> RunResult:
        calls["n"] += 1
        if calls["n"] == 1:
            await coordinator.signal_pause(reason="ctrl-c")
            return RunResult.PAUSED
        return RunResult.COMPLETED

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_quest_session(
                quest, run_fn=fake_run, hibernate=False)

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        seen1: list[dict[str, Any]] = []
        c1 = SessionClient(sock, quest.id, quest.title, on_event=seen1.append)
        await c1.connect()
        tg.start_soon(c1.receive_events)
        await wait_until(lambda: sum(e.get("type") == "awaiting_user_input" for e in seen1) >= 1)
        # Late client attaches at the resume gate.
        seen2: list[dict[str, Any]] = []
        c2 = SessionClient(sock, quest.id, quest.title, on_event=seen2.append)
        await c2.connect()
        tg.start_soon(c2.receive_events)
        await wait_until(lambda: sum(e.get("type") == "awaiting_user_input" for e in seen2) >= 1)
        await anyio.sleep(0.1)
        assert sum(e.get("type") == "awaiting_user_input" for e in seen1) == 1
        await c2.send_input("go")
        await wait_until(lambda: "r" in result)
        assert result["r"] is RunResult.COMPLETED
        await c1.aclose()
        await c2.aclose()
        tg.cancel_scope.cancel()


def test_is_interrupt_pause_true_only_for_paused_with_should_pause() -> None:
    paused_interrupt = SimpleNamespace(should_pause=True)
    paused_workflow = SimpleNamespace(should_pause=False)
    assert _is_interrupt_pause(RunResult.PAUSED, paused_interrupt) is True
    assert _is_interrupt_pause(RunResult.PAUSED, paused_workflow) is False
    assert _is_interrupt_pause(RunResult.COMPLETED, paused_interrupt) is False


@pytest.mark.anyio
async def test_interrupt_pause_reprompts_and_resumes_in_place(
    tmp_path: Path,
) -> None:
    quest = _make_change(tmp_path, "2026-06-15-rzm")
    sock = str(session_socket_path(quest.control_root, quest.id))
    calls = {"n": 0}

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> RunResult:
        calls["n"] += 1
        if calls["n"] == 1:
            await coordinator.signal_pause(reason="ctrl-c")
            return RunResult.PAUSED
        assert not coordinator.should_pause  # reset() must have cleared this
        on_event({"type": "agent_result",
                  "payload": {"redirect": coordinator.drain_interject()}})
        return RunResult.COMPLETED

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_quest_session(
                quest, run_fn=fake_run, hibernate=False)

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        received: list[dict[str, Any]] = []
        client = SessionClient(sock, quest.id, quest.title,
                               on_event=received.append)
        await client.connect()
        tg.start_soon(client.receive_events)
        await wait_until(lambda: any(e.get("type") == "awaiting_user_input" for e in received))
        await client.send_input("go left")
        await wait_until(lambda: "r" in result)
        assert result["r"] is RunResult.COMPLETED
        assert calls["n"] == 2
        assert {"type": "agent_result",
                "payload": {"redirect": "go left"}} in received
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_interrupt_pause_hibernates_with_zero_clients(
    tmp_path: Path,
) -> None:
    quest = _make_change(tmp_path, "2026-06-15-rzh")
    calls = {"n": 0}

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> RunResult:
        calls["n"] += 1
        await coordinator.signal_pause(reason="ctrl-c")
        return RunResult.PAUSED

    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(
            quest, run_fn=fake_run, hibernate=True, first_attach_timeout=0.1)
    assert result is RunResult.PAUSED
    assert calls["n"] == 1


@pytest.mark.anyio
async def test_resume_journals_workflow_resumed_without_flipping_status(
    tmp_path: Path,
) -> None:
    from imp.engine.state import State
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    st = State.load(quest.state_dir)
    st.status = "paused"
    st.save(quest.state_dir)

    seen: dict[str, str] = {}

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> RunResult:
        seen["status"] = State.load(quest.state_dir).status
        return RunResult.COMPLETED

    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(
            quest, run_fn=fake_run, hibernate=False, resume=True)

    assert result is RunResult.COMPLETED
    assert seen["status"] == "paused"           # never flipped to a persisted "running"
    assert quest.events_path.exists() and "workflow_resumed" in quest.events_path.read_text()


@pytest.mark.anyio
async def test_resume_sets_resume_pending_for_a_stale_lock_crash(tmp_path: Path) -> None:
    """A hard-killed run leaves a lock file naming a now-dead pid (the OS frees
    the flock but not the pid file). The pre-acquire snapshot reads that as a
    crash and sets resume_pending to the current step so the executor replays it."""
    import subprocess

    from imp.engine.state import State
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    st = State.load(quest.state_dir)
    st.step = "review"
    st.status = "paused"
    st.resume_pending = None
    st.save(quest.state_dir)
    p = subprocess.Popen(["true"])           # dead pid ⇒ stale lock ⇒ crash
    p.wait()
    (quest.quest_dir / "lock").write_text(str(p.pid))

    seen: dict[str, object] = {}

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> RunResult:
        st = State.load(quest.state_dir)
        seen["resume_pending"] = st.resume_pending
        seen["resume_mode"] = st.resume_mode
        return RunResult.COMPLETED

    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(
            quest, run_fn=fake_run, hibernate=False, resume=True)

    assert result is RunResult.COMPLETED
    assert seen["resume_pending"] == "review"
    assert seen["resume_mode"] == "continue"   # crash recovery continues the step


@pytest.mark.anyio
async def test_resume_with_no_lock_does_not_set_resume_pending(tmp_path: Path) -> None:
    """A clean pause leaves no lock file: resume must NOT replay (resume_pending
    stays unset) so the run advances normally."""
    from imp.engine.state import State
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    st = State.load(quest.state_dir)
    st.step = "review"
    st.status = "paused"
    st.resume_pending = None
    st.save(quest.state_dir)

    seen: dict[str, object] = {}

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> RunResult:
        seen["resume_pending"] = State.load(quest.state_dir).resume_pending
        return RunResult.COMPLETED

    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(
            quest, run_fn=fake_run, hibernate=False, resume=True)

    assert result is RunResult.COMPLETED
    assert seen["resume_pending"] is None


@pytest.mark.anyio
async def test_default_spawn_does_no_resume_prep(tmp_path: Path) -> None:
    from imp.engine.state import State
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    st = State.load(quest.state_dir)
    st.status = "paused"
    st.save(quest.state_dir)

    seen: dict[str, str] = {}

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> RunResult:
        seen["status"] = State.load(quest.state_dir).status
        return RunResult.COMPLETED

    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(
            quest, run_fn=fake_run, hibernate=False)  # resume defaults False

    assert result is RunResult.COMPLETED
    assert seen["status"] == "paused"           # untouched: no resume prep ran
    assert not quest.events_path.exists() or (
        "workflow_resumed" not in quest.events_path.read_text())


def test_main_forwards_resume_flag_via_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import functools

    import imp.remote.session.daemon as daemon

    loaded = SimpleNamespace(quest_dir=tmp_path)
    captured: dict[str, Any] = {}

    def fake_load(control_root: Path, quest_id: str) -> object:
        return loaded

    def fake_run(fn: Any) -> None:   # partial form takes a single positional
        captured["fn"] = fn

    monkeypatch.setattr(daemon.Quest, "load", staticmethod(fake_load))
    monkeypatch.setattr(daemon.anyio, "run", fake_run)

    rc = daemon.main([
        "--control-root", str(tmp_path),
        "--quest-id", "2026-06-15-abc",
        "--resume",
    ])

    assert rc == 0
    fn = captured["fn"]
    assert isinstance(fn, functools.partial)
    assert fn.func is daemon.serve_quest_session
    assert fn.args == (loaded,)
    assert fn.keywords == {"resume": True}


def test_append_workflow_resumed_is_idempotent_when_trailing(tmp_path: Path) -> None:
    import imp.remote.session.daemon as daemon
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    log = quest.event_log()
    daemon._append_workflow_resumed(log)
    daemon._append_workflow_resumed(log)   # trailing already resumed → no-op
    assert [e["type"] for e in log.read()].count("workflow_resumed") == 1


@pytest.mark.anyio
async def test_two_interrupt_rounds_leave_no_open_pause(tmp_path: Path) -> None:
    from imp.cli.signals import _open_pause_exists

    quest = _make_change(tmp_path, "2026-06-15-bal")
    calls = {"n": 0}

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> RunResult:
        calls["n"] += 1
        if calls["n"] <= 2:
            quest.event_log().append(
                agent_id=None, type="workflow_paused",
                payload={"reason": "interrupt"})
            await coordinator.signal_pause(reason="ctrl-c")
            return RunResult.PAUSED
        return RunResult.COMPLETED

    sock = str(session_socket_path(quest.control_root, quest.id))
    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_quest_session(
                quest, run_fn=fake_run, hibernate=False)

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        seen: list[dict[str, Any]] = []
        client = SessionClient(sock, quest.id, quest.title, on_event=seen.append)
        await client.connect()
        tg.start_soon(client.receive_events)

        async def _answer() -> None:
            answered = 0
            while "r" not in result:
                prompts = sum(e.get("type") == "awaiting_user_input" for e in seen)
                if prompts > answered:
                    await client.send_input("go")
                    answered += 1
                await anyio.sleep(0.02)
        tg.start_soon(_answer)
        await wait_until(lambda: "r" in result)
        assert result["r"] is RunResult.COMPLETED
        types = [e["type"] for e in quest.event_log().read()]
        assert types.count("workflow_paused") == 2
        assert types.count("workflow_resumed") == 2
        assert not _open_pause_exists(quest)
        await client.aclose()
        tg.cancel_scope.cancel()



def test_main_unknown_quest_id_prints_clean_error_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.remote.session.daemon as daemon
    from imp.engine.errors import LoadError

    def boom(_root: Path, _cid: str) -> object:
        raise LoadError("no such change feat")

    monkeypatch.setattr(daemon.Quest, "load", staticmethod(boom))
    rc = daemon.main(["--control-root", str(tmp_path), "--quest-id", "feat"])
    assert rc == 2
    assert "no such change feat" in capsys.readouterr().err


def test_main_maps_paused_outcome_to_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.remote.session.daemon as daemon
    from imp.engine.executor import RunResult

    monkeypatch.setattr(
        daemon.Quest, "load",
        staticmethod(lambda r, c: SimpleNamespace(quest_dir=tmp_path)))
    monkeypatch.setattr(daemon.anyio, "run", lambda *a, **k: RunResult.PAUSED)
    rc = daemon.main(["--control-root", str(tmp_path), "--quest-id", "feat"])
    assert rc == 1


def test_main_maps_paused_outcome_to_nonzero_with_kwargs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.remote.session.daemon as daemon
    from imp.engine.executor import RunResult

    monkeypatch.setattr(
        daemon.Quest, "load",
        staticmethod(lambda r, c: SimpleNamespace(quest_dir=tmp_path)))
    monkeypatch.setattr(daemon.anyio, "run", lambda *a, **k: RunResult.PAUSED)
    rc = daemon.main(["--control-root", str(tmp_path), "--quest-id", "feat", "--resume"])
    assert rc == 1



def test_append_workflow_resumed_appends_after_a_real_event(tmp_path: Path) -> None:
    import imp.remote.session.daemon as daemon
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    log = quest.event_log()
    daemon._append_workflow_resumed(log)
    log.append(agent_id=None, type="workflow_paused", payload={"reason": "interrupt"})
    daemon._append_workflow_resumed(log)   # real event between → a second marker
    assert [e["type"] for e in log.read()].count("workflow_resumed") == 2


def test_last_pause_was_awaiting_turn_true_for_awaiting_turn(tmp_path: Path) -> None:
    import imp.remote.session.daemon as daemon
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    quest.event_log().append(
        agent_id=None, type="workflow_paused", payload={"reason": "awaiting_turn"})
    assert daemon._last_pause_was_awaiting_turn(quest.event_log()) is True


def test_last_pause_was_awaiting_turn_false_for_other_reason(tmp_path: Path) -> None:
    import imp.remote.session.daemon as daemon
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    quest.event_log().append(
        agent_id=None, type="workflow_paused", payload={"reason": "interrupt"})
    assert daemon._last_pause_was_awaiting_turn(quest.event_log()) is False


def test_last_pause_was_awaiting_turn_false_after_resume(tmp_path: Path) -> None:
    import imp.remote.session.daemon as daemon
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    log = quest.event_log()
    log.append(agent_id=None, type="workflow_paused", payload={"reason": "awaiting_turn"})
    log.append(agent_id=None, type="workflow_resumed", payload={})
    assert daemon._last_pause_was_awaiting_turn(log) is False   # pause is closed



def test_last_pause_was_awaiting_turn_false_with_no_events(tmp_path: Path) -> None:
    import imp.remote.session.daemon as daemon
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    # No events at all — the fallback must be False
    assert daemon._last_pause_was_awaiting_turn(quest.event_log()) is False


@pytest.mark.anyio
async def test_resident_abandon_of_unmaterialized_draft_tears_down(
    tmp_path: Path,
) -> None:
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    quest = Quest.create_draft(
        tmp_path, title="x", quest_id="draft1", base_branch="main")
    assert quest.materialized is False
    sock = str(session_socket_path(quest.control_root, quest.id))
    torn: list[Quest] = []
    archived: list[Quest] = []

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> str:
        await anyio.sleep_forever()   # only an abandon-cancel ends this run
        return "x"

    async with anyio.create_task_group() as tg:
        async def _serve() -> None:
            await serve_quest_session(
                quest, run_fn=fake_run, hibernate=False,
                tear_down=lambda ch: torn.append(ch),
                archive_left=lambda ch: archived.append(ch))

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        client = SessionClient(sock, quest.id, quest.title,
                               on_event=lambda _e: None)
        await client.connect()
        await client.send_control(CONTROL_ABANDON)
        await wait_until(lambda: torn)
        assert torn == [quest]
        assert archived == []  # the resumable archive path is not taken
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_awaiting_turn_hibernation_exit_drives_now_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.remote.session.attach as attach_mod
    from imp.engine import merge_queue as mq
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="b", quest_id="bbb", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "bbb", "main")        # bbb is now the lane head

    spawned: list = []
    monkeypatch.setattr(
        attach_mod, "spawn_session_daemon",
        lambda ch, **kw: spawned.append((ch.id, kw.get("resume"))))

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        quest.event_log().append(
            agent_id=None, type="workflow_paused", payload={"reason": "awaiting_turn"})
        return RunResult.PAUSED

    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(quest, run_fn=fake_run, hibernate=False)

    assert result is RunResult.PAUSED
    assert spawned == [("bbb", True)]    # hook re-drove the now-head after lock release


@pytest.mark.anyio
async def test_non_awaiting_turn_pause_does_not_drive_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.remote.session.attach as attach_mod
    from imp.engine import merge_queue as mq
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="b", quest_id="bbb", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "bbb", "main")        # bbb IS head, yet no spawn

    spawned: list = []
    monkeypatch.setattr(
        attach_mod, "spawn_session_daemon", lambda ch, **kw: spawned.append(ch.id))

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        quest.event_log().append(
            agent_id=None, type="workflow_paused", payload={"reason": "interrupt"})
        return RunResult.PAUSED

    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(quest, run_fn=fake_run, hibernate=False)

    assert result is RunResult.PAUSED
    assert spawned == []   # a user-pause exit suppresses the handoff-repair hook


@pytest.mark.anyio
async def test_resident_save_applies_the_save_disposition(tmp_path: Path) -> None:
    quest = _make_change(tmp_path, "2026-06-15-sav")
    sock = str(session_socket_path(quest.control_root, quest.id))
    saved: list[Quest] = []
    torn: list[Quest] = []

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> None:
        await anyio.sleep_forever()   # only a save-cancel ends this run

    async with anyio.create_task_group() as tg:
        async def _serve() -> None:
            await serve_quest_session(
                quest, run_fn=fake_run, hibernate=False,
                tear_down=lambda ch: torn.append(ch),
                save_left=lambda ch, reason=None: saved.append(ch))

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        client = SessionClient(sock, quest.id, quest.title,
                               on_event=lambda _e: None)
        await client.connect()
        await client.send_control(CONTROL_SAVE)
        await wait_until(lambda: saved)
        assert saved == [quest]
        assert torn == []
        await client.aclose()
        tg.cancel_scope.cancel()



def test_hold_prompt_resume_prompt_for_interrupt() -> None:
    import imp.remote.session.daemon as daemon

    coord = SimpleNamespace(should_pause=True, pause_reason="ctrl-c")
    assert daemon._hold_prompt(RunResult.PAUSED, coord) == daemon._RESUME_PROMPT


def test_hold_prompt_none_for_model_override() -> None:
    import imp.remote.session.daemon as daemon

    coord = SimpleNamespace(should_pause=True, pause_reason="model-override")
    assert daemon._hold_prompt(RunResult.PAUSED, coord) is None


def test_hold_prompt_returns_resume_for_sandbox_override_style_interrupt() -> None:
    # "sandbox-override" is no longer excluded from _hold_prompt's guard: since
    # CONTROL_SET_SANDBOX no longer calls signal_pause, the scenario can't arise
    # in production, but the guard itself must not suppress arbitrary interrupt reasons.
    import imp.remote.session.daemon as daemon

    coord = SimpleNamespace(should_pause=True, pause_reason="sandbox-override")
    assert daemon._hold_prompt(RunResult.PAUSED, coord) is not None


def test_hold_prompt_none_for_script_blocked() -> None:
    import imp.remote.session.daemon as daemon

    coord = SimpleNamespace(should_pause=False)
    assert daemon._hold_prompt(RunResult.PAUSED, coord) is None


def test_hold_prompt_none_for_awaiting_turn(tmp_path: Path) -> None:
    import imp.remote.session.daemon as daemon
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    quest.event_log().append(
        agent_id=None, type="workflow_paused", payload={"reason": "awaiting_turn"})
    coord = SimpleNamespace(should_pause=False)
    assert daemon._hold_prompt(RunResult.PAUSED, coord) is None


@pytest.mark.anyio
async def test_script_blocked_pause_exits_terminal(tmp_path: Path) -> None:
    quest = _make_change(tmp_path, "2026-06-15-scb")
    calls = {"n": 0}

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        calls["n"] += 1
        log = quest.event_log()
        log.append(agent_id=None, type="script_blocked",
                   payload={"step": "integrate", "exit": 3, "stdout": "",
                            "stderr": "integration blocked: worktree dirty"})
        log.append(agent_id=None, type="workflow_paused",
                   payload={"reason": "script_blocked"})
        return RunResult.PAUSED

    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(quest, run_fn=fake_run, hibernate=False)
    assert result is RunResult.PAUSED
    assert calls["n"] == 1  # exited terminal — never looped


@pytest.mark.anyio
async def test_script_blocked_pause_hibernates_with_zero_clients(
    tmp_path: Path,
) -> None:
    quest = _make_change(tmp_path, "2026-06-15-sch")
    calls = {"n": 0}

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        calls["n"] += 1
        log = quest.event_log()
        log.append(agent_id=None, type="script_blocked",
                   payload={"step": "integrate", "exit": 3, "stdout": "",
                            "stderr": "blocked"})
        log.append(agent_id=None, type="workflow_paused",
                   payload={"reason": "script_blocked"})
        return RunResult.PAUSED

    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(
            quest, run_fn=fake_run, hibernate=True, first_attach_timeout=0.1)
    assert result is RunResult.PAUSED
    assert calls["n"] == 1   # held once, then hibernated — never looped


@pytest.mark.anyio
async def test_resident_archive_applies_the_archive_disposition(tmp_path: Path) -> None:
    quest = _make_change(tmp_path, "2026-06-15-arc")
    sock = str(session_socket_path(quest.control_root, quest.id))
    archived: list[Quest] = []
    discarded: list[Quest] = []

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> None:
        await anyio.sleep_forever()

    async with anyio.create_task_group() as tg:
        async def _serve() -> None:
            await serve_quest_session(
                quest, run_fn=fake_run, hibernate=False,
                archive_left=lambda ch: archived.append(ch),
                discard_left=lambda ch: discarded.append(ch))

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        client = SessionClient(sock, quest.id, quest.title,
                               on_event=lambda _e: None)
        await client.connect()
        await client.send_control(CONTROL_ARCHIVE)
        await wait_until(lambda: archived)
        assert archived == [quest]
        assert discarded == []
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_resident_discard_applies_the_discard_disposition(tmp_path: Path) -> None:
    quest = _make_change(tmp_path, "2026-06-15-dis")
    sock = str(session_socket_path(quest.control_root, quest.id))
    archived: list[Quest] = []
    discarded: list[Quest] = []

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> None:
        await anyio.sleep_forever()

    async with anyio.create_task_group() as tg:
        async def _serve() -> None:
            await serve_quest_session(
                quest, run_fn=fake_run, hibernate=False,
                archive_left=lambda ch: archived.append(ch),
                discard_left=lambda ch: discarded.append(ch))

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        client = SessionClient(sock, quest.id, quest.title,
                               on_event=lambda _e: None)
        await client.connect()
        await client.send_control(CONTROL_DISCARD)
        await wait_until(lambda: discarded)
        assert discarded == [quest]
        assert archived == []
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_serve_forwards_non_interactive_to_run_quest(tmp_path: Path) -> None:
    quest = _make_change(tmp_path, "2026-06-15-nint")
    captured: dict[str, Any] = {}

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
        headless: bool = False,
    ) -> str:
        captured["headless"] = headless
        return "done"

    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(
            quest, run_fn=fake_run, hibernate=False, non_interactive=True)
    assert result == "done"
    assert captured["headless"] is True


def test_spawn_session_daemon_emits_non_interactive_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    import imp.remote.session.attach as attach_mod

    quest = _make_change(tmp_path, "2026-06-15-spwn")
    captured: dict[str, Any] = {}

    class _FakePopen:
        def __init__(self, argv: list[str], **_kw: Any) -> None:
            captured["argv"] = argv

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    attach_mod.spawn_session_daemon(quest, non_interactive=True)
    assert "--non-interactive" in captured["argv"]


def test_spawn_session_daemon_omits_flag_when_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    import imp.remote.session.attach as attach_mod

    quest = _make_change(tmp_path, "2026-06-15-spwi")
    captured: dict[str, Any] = {}

    class _FakePopen:
        def __init__(self, argv: list[str], **_kw: Any) -> None:
            captured["argv"] = argv

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    attach_mod.spawn_session_daemon(quest)
    assert "--non-interactive" not in captured["argv"]


def test_main_parses_non_interactive_into_serve_kwargs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.remote.session.daemon as daemon

    loaded = SimpleNamespace(quest_dir=tmp_path)
    calls: dict[str, Any] = {}
    monkeypatch.setattr(daemon.Quest, "load",
                        staticmethod(lambda cr, cid: loaded))

    def fake_run(fn: Any) -> None:
        calls["fn"] = fn

    monkeypatch.setattr(daemon.anyio, "run", fake_run)
    rc = daemon.main([
        "--control-root", str(tmp_path),
        "--quest-id", "2026-06-15-mnint", "--non-interactive"])
    assert rc == 0
    fn = calls["fn"]
    assert fn.func is daemon.serve_quest_session
    assert fn.keywords == {"non_interactive": True}


def test_build_session_attach_forwards_non_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.remote.session.attach as attach_mod

    quest = _make_change(tmp_path, "2026-06-15-bsa")
    captured: dict[str, Any] = {}

    def fake_spawn(quest: Quest, **kw: Any) -> None:
        captured.update(kw)

    monkeypatch.setattr(attach_mod, "spawn_session_daemon", fake_spawn)

    holder: dict[str, Any] = {}

    async def fake_attach_or_spawn(
        quest: Quest, *, on_event: Any, spawn_fn: Any,
        connect_timeout: float = 10.0, lease: bool = False,
    ) -> object:
        holder["spawn_fn"] = spawn_fn
        return object()

    monkeypatch.setattr(attach_mod, "attach_or_spawn", fake_attach_or_spawn)

    attach = attach_mod.build_session_attach(quest, non_interactive=True)
    anyio.run(attach, lambda _e: None)
    holder["spawn_fn"](quest)
    assert captured["non_interactive"] is True


def test_build_session_attach_forwards_autonomous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.remote.session.attach as attach_mod

    quest = _make_change(tmp_path, "2026-06-15-bsa-auto")
    captured: dict[str, Any] = {}

    def fake_spawn(quest: Quest, **kw: Any) -> None:
        captured.update(kw)

    monkeypatch.setattr(attach_mod, "spawn_session_daemon", fake_spawn)

    holder: dict[str, Any] = {}

    async def fake_attach_or_spawn(
        quest: Quest, *, on_event: Any, spawn_fn: Any,
        connect_timeout: float = 10.0, lease: bool = False,
    ) -> object:
        holder["spawn_fn"] = spawn_fn
        return object()

    monkeypatch.setattr(attach_mod, "attach_or_spawn", fake_attach_or_spawn)

    attach = attach_mod.build_session_attach(quest, autonomous=True)
    anyio.run(attach, lambda _e: None)
    holder["spawn_fn"](quest)
    assert captured["autonomous"] is True


@pytest.mark.anyio
async def test_resume_loop_under_non_interactive_passes_flag_to_second_run(
    tmp_path: Path,
) -> None:
    quest = _make_change(tmp_path, "2026-06-15-nirl")
    sock = str(session_socket_path(quest.control_root, quest.id))
    received_flags: list[bool] = []

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
        headless: bool = False,
    ) -> RunResult:
        received_flags.append(headless)
        if len(received_flags) == 1:
            await coordinator.signal_pause(reason="ctrl-c")
            return RunResult.PAUSED
        return RunResult.COMPLETED

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_quest_session(
                quest, run_fn=fake_run, hibernate=False, non_interactive=True)

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        received: list[dict[str, Any]] = []
        client = SessionClient(sock, quest.id, quest.title,
                               on_event=received.append)
        await client.connect()
        tg.start_soon(client.receive_events)
        await wait_until(lambda: any(e.get("type") == "awaiting_user_input" for e in received))
        await client.send_input("redirect")
        await wait_until(lambda: "r" in result)
        assert result["r"] is RunResult.COMPLETED
        assert received_flags == [True, True]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_resume_loop_under_autonomous_passes_flag_to_second_run(
    tmp_path: Path,
) -> None:
    quest = _make_change(tmp_path, "2026-06-15-arl")
    sock = str(session_socket_path(quest.control_root, quest.id))
    received_flags: list[bool] = []

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
        autonomous: bool = False,
    ) -> RunResult:
        received_flags.append(autonomous)
        if len(received_flags) == 1:
            await coordinator.signal_pause(reason="ctrl-c")
            return RunResult.PAUSED
        return RunResult.COMPLETED

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_quest_session(
                quest, run_fn=fake_run, hibernate=False, autonomous=True)

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        received: list[dict[str, Any]] = []
        client = SessionClient(sock, quest.id, quest.title,
                               on_event=received.append)
        await client.connect()
        tg.start_soon(client.receive_events)
        await wait_until(lambda: any(e.get("type") == "awaiting_user_input" for e in received))
        await client.send_input("redirect")
        await wait_until(lambda: "r" in result)
        assert result["r"] is RunResult.COMPLETED
        assert received_flags == [True, True]
        await client.aclose()
        tg.cancel_scope.cancel()


def test_main_forwards_autonomous_via_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import functools

    import imp.remote.session.daemon as daemon

    loaded = SimpleNamespace(quest_dir=tmp_path)
    captured: dict[str, Any] = {}

    def fake_load(control_root: Path, quest_id: str) -> object:
        return loaded

    def fake_run(fn: Any) -> None:
        captured["fn"] = fn

    monkeypatch.setattr(daemon.Quest, "load", staticmethod(fake_load))
    monkeypatch.setattr(daemon.anyio, "run", fake_run)

    rc = daemon.main([
        "--control-root", str(tmp_path),
        "--quest-id", "2026-06-15-abc",
        "--autonomous",
    ])

    assert rc == 0
    fn = captured["fn"]
    assert isinstance(fn, functools.partial)
    assert fn.func is daemon.serve_quest_session
    assert fn.keywords == {"autonomous": True}


@pytest.mark.anyio
async def test_autonomous_forwarded_to_run_fn(tmp_path: Path) -> None:
    quest = _make_change(tmp_path, "2026-06-15-aut")
    captured: dict[str, Any] = {}

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None, autonomous: bool = False,
    ) -> RunResult:
        captured["autonomous"] = autonomous
        return RunResult.COMPLETED

    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(
            quest, run_fn=fake_run, hibernate=False, autonomous=True)
    assert result is RunResult.COMPLETED
    assert captured["autonomous"] is True


@pytest.mark.anyio
async def test_detach_at_hold_prompt_stamps_resume_target_prompt(
    tmp_path: Path,
) -> None:
    quest = _make_change(tmp_path, "2026-06-15-dph")
    st = State.load(quest.state_dir)
    st.step = "code"               # the active step at detach time
    st.save(quest.state_dir)

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        await coordinator.signal_pause(reason="ctrl-c")
        return RunResult.PAUSED

    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(
            quest, run_fn=fake_run, hibernate=True, first_attach_timeout=0.1)

    assert result is RunResult.PAUSED
    state = State.load(quest.state_dir)
    assert state.resume_target == "prompt-paused"
    assert state.resume_pending == "code"    # non-ctrl-d branch arms solicit on state.step
    assert state.resume_mode == "solicit"    # detaching at the hold prompt re-solicits


@pytest.mark.anyio
async def test_detach_at_interrupt_steer_stamps_prompt_interrupted(
    tmp_path: Path,
) -> None:
    quest = _make_change(tmp_path, "2026-06-15-dis")
    st = State.load(quest.state_dir)
    st.step = "code"               # the active step at detach time
    st.save(quest.state_dir)

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        # The executor arms the interrupt disposition before unwinding to the
        # daemon hold (mid-turn Ctrl-C steer: Pause(interrupt=True) ->
        # arm_step_resume(mode="continue")).
        s = State.load(quest.state_dir)
        s.arm_step_resume(mode="continue")
        s.save(quest.state_dir)
        await coordinator.signal_pause(reason="ctrl-c")
        return RunResult.PAUSED

    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(
            quest, run_fn=fake_run, hibernate=True, first_attach_timeout=0.1)

    assert result is RunResult.PAUSED
    state = State.load(quest.state_dir)
    assert state.resume_target == "prompt-interrupted"
    assert state.resume_pending == "code"    # executor's arming is preserved
    assert state.resume_mode == "continue"   # NOT overwritten with solicit


@pytest.mark.anyio
async def test_detach_after_ctrl_d_midrun_stamps_resume_target_agent(
    tmp_path: Path,
) -> None:
    quest = _make_change(tmp_path, "2026-06-15-cda")
    st = State.load(quest.state_dir)
    st.step = "review"
    st.save(quest.state_dir)

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        await coordinator.signal_pause(reason="ctrl-d")
        return RunResult.PAUSED

    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(
            quest, run_fn=fake_run, hibernate=True, first_attach_timeout=0.1)

    assert result is RunResult.PAUSED
    state = State.load(quest.state_dir)
    assert state.resume_target == "agent"
    assert state.resume_pending == "review"   # arm_step_resume(step) → native resume
    assert state.resume_mode == "continue"    # ctrl-d detach continues on reattach


@pytest.mark.timeout(240)  # raise the 60s cap so inner backstops can't false-fire under -n auto
@pytest.mark.parametrize("target,prompt_attr", [
    ("prompt-paused", "_RESUME_PROMPT"),
    ("prompt-interrupted", "_RESUME_INTERRUPTED_PROMPT"),
])
@pytest.mark.anyio
async def test_resume_prompt_target_holds_before_running_agent(
    tmp_path: Path, target: str, prompt_attr: str,
) -> None:
    import imp.remote.session.daemon as daemon

    quest = _make_change(tmp_path, "2026-06-15-rpt")
    st = State.load(quest.state_dir)
    st.resume_target = target
    st.save(quest.state_dir)
    sock = str(session_socket_path(quest.control_root, quest.id))

    calls: dict[str, Any] = {"n": 0, "interject": None}
    run_done = anyio.Event()

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        calls["n"] += 1
        calls["interject"] = coordinator.drain_interject()
        on_event({"type": "agent_result", "payload": {}})
        return RunResult.COMPLETED

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_quest_session(
                quest, run_fn=fake_run, hibernate=False, resume=True)
            run_done.set()

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        received: list[dict[str, Any]] = []
        gate_seen = anyio.Event()

        def _on_event(e: dict[str, Any]) -> None:
            received.append(e)
            if e.get("type") == "awaiting_user_input":
                gate_seen.set()

        client = SessionClient(sock, quest.id, quest.title,
                               on_event=_on_event)
        await client.connect()
        tg.start_soon(client.receive_events)
        await gate_seen.wait()
        # The hold prompt is surfaced FIRST — the agent has not run yet.
        assert calls["n"] == 0
        gates = [e for e in received if e.get("type") == "awaiting_user_input"]
        prompts = [e["payload"]["steer_hint"] for e in gates]
        assert prompts[-1] == getattr(daemon, prompt_attr)
        # The hold-loop resteer prompt is self-inflicted: flag it so Telegram
        # curation silences it instead of buzzing the operator.
        assert gates[-1]["payload"]["interrupt"] is True
        await client.send_input("steer me")
        await run_done.wait()
        assert result["r"] is RunResult.COMPLETED
        assert calls["n"] == 1
        assert calls["interject"] == "steer me"
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
@pytest.mark.parametrize("target", [None, "agent"])
async def test_resume_agent_target_runs_straight_away(
    tmp_path: Path, target: str | None,
) -> None:
    quest = _make_change(tmp_path, "2026-06-15-rat")
    st = State.load(quest.state_dir)
    st.resume_target = target
    st.save(quest.state_dir)

    seen: dict[str, bool] = {"ran": False}

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        seen["ran"] = True
        return RunResult.COMPLETED

    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(
            quest, run_fn=fake_run, hibernate=False, resume=True)

    assert result is RunResult.COMPLETED
    assert seen["ran"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("flag", ["non_interactive", "autonomous"])
@pytest.mark.parametrize("target", ["prompt-interrupted", "prompt-paused"])
async def test_resume_prompt_target_runs_agent_without_interactive_channel(
    tmp_path: Path, flag: str, target: str,
) -> None:
    """A prompt-* resteer hold only makes sense when a human can
    type. A headless or autonomous resume has no channel to answer the prompt, so
    honoring it would re-pause without progress — it must continue the agent
    instead (§6.15 mid-step ctrl-c resume completes the interrupted step)."""
    quest = _make_change(tmp_path, "2026-06-15-rph")
    st = State.load(quest.state_dir)
    st.resume_target = target
    st.save(quest.state_dir)

    seen: dict[str, bool] = {"ran": False}

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
        headless: bool = False, autonomous: bool = False,
    ) -> RunResult:
        seen["ran"] = True
        return RunResult.COMPLETED

    kwargs = {flag: True}
    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(
            quest, run_fn=fake_run, hibernate=True, resume=True,
            first_attach_timeout=0.1, **kwargs)

    assert result is RunResult.COMPLETED
    assert seen["ran"] is True


@pytest.mark.anyio
async def test_interrupt_leave_control_pauses_with_ctrl_d_reason(tmp_path: Path) -> None:
    from imp.remote.session.protocol import CONTROL_INTERRUPT_LEAVE
    from imp.remote.session.server import SessionServer

    class _FakeCoord:
        def __init__(self) -> None:
            self.reasons: list[str] = []

        def set_save_hook(self, fn: Any) -> None:
            pass

        async def signal_pause(self, *, reason: str = "ctrl-c",
                               via: str = "signal") -> None:
            self.reasons.append(reason)

    coord = _FakeCoord()
    server = SessionServer(str(tmp_path / "unused.sock"), coordinator=coord)
    await server._dispatch_control(CONTROL_INTERRUPT_LEAVE)
    assert coord.reasons == ["ctrl-d"]


@pytest.mark.anyio
async def test_interrupt_leave_control_passes_frame_reason_through(tmp_path: Path) -> None:
    from imp.remote.session.protocol import CONTROL_INTERRUPT_LEAVE
    from imp.remote.session.server import SessionServer

    class _FakeCoord:
        def __init__(self) -> None:
            self.reasons: list[str] = []

        def set_save_hook(self, fn: Any) -> None:
            pass

        async def signal_pause(self, *, reason: str = "ctrl-c",
                               via: str = "signal") -> None:
            self.reasons.append(reason)

    coord = _FakeCoord()
    server = SessionServer(str(tmp_path / "unused.sock"), coordinator=coord)
    # A stamped trigger passes through verbatim…
    await server._dispatch_control(CONTROL_INTERRUPT_LEAVE, "sigterm")
    # …and a frame with no reason still defaults to the ctrl-d fallback.
    await server._dispatch_control(CONTROL_INTERRUPT_LEAVE)
    assert coord.reasons == ["sigterm", "ctrl-d"]


@pytest.mark.anyio
@pytest.mark.parametrize("reason", ["ctrl-d", "sigterm", "sighup", "sigquit"])
async def test_leave_frame_reason_wins_over_lease_drop(tmp_path: Path, reason: str) -> None:
    from imp.cli.signals import InterruptCoordinator
    from imp.remote.session.protocol import CONTROL_INTERRUPT_LEAVE
    from imp.remote.session.server import _INTERNAL_PAUSE, SessionServer

    coord = InterruptCoordinator()
    server = SessionServer(str(tmp_path / "unused.sock"), coordinator=coord)
    # The deliberate-leave frame carries the trigger and lands first; the
    # subsequent lease-drop pause cannot overwrite it (signal_pause is first-wins).
    await server._dispatch_control(CONTROL_INTERRUPT_LEAVE, reason)
    await server._dispatch_control(_INTERNAL_PAUSE)
    assert coord.pause_reason == reason


@pytest.mark.anyio
async def test_lease_drop_without_a_gesture_records_detached(tmp_path: Path) -> None:
    from imp.cli.signals import InterruptCoordinator
    from imp.remote.session.server import _INTERNAL_PAUSE, SessionServer

    coord = InterruptCoordinator()
    server = SessionServer(str(tmp_path / "unused.sock"), coordinator=coord)
    # No leave frame — a client that vanished without a gesture (lease drop).
    await server._dispatch_control(_INTERNAL_PAUSE)
    assert coord.pause_reason == "detached"


@pytest.mark.anyio
async def test_dispatch_set_model_stashes_arg_and_pauses(tmp_path: Path) -> None:
    from imp.remote.session.protocol import CONTROL_SET_MODEL
    from imp.remote.session.server import SessionServer

    calls: list[tuple[str, str]] = []

    class _Coord:
        def __init__(self) -> None:
            self.pending: str | None = None

        def set_save_hook(self, fn: Any) -> None:
            pass

        def set_pending_model(self, arg: str) -> None:
            self.pending = arg

        async def signal_pause(self, *, reason: str = "ctrl-c",
                               via: str = "signal") -> None:
            calls.append((reason, via))

    coord = _Coord()
    server = SessionServer(str(tmp_path / "unused.sock"), coordinator=coord)

    await server._dispatch_control(CONTROL_SET_MODEL, "anthropic:claude-fable-5")

    assert coord.pending == "anthropic:claude-fable-5"
    assert calls == [("model-override", "frame")]
    # Ordinary control: serve() is never cancelled by set-model.
    assert server.abandoned is server.saved is server.archived is server.discarded is False


@pytest.mark.anyio
async def test_model_override_pause_reenters_without_resume_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.remote.session.daemon as daemon_mod

    quest = _make_change(tmp_path, "2026-06-15-mdl")

    applied: list[tuple[str, str]] = []
    monkeypatch.setattr(
        daemon_mod, "apply_model_override",
        lambda ch, state, arg, *, step_name: applied.append((arg, step_name)))

    calls = {"n": 0}

    async def _run_fn(
        ch: Quest, *, on_event: Any, user_chat_input_fn: Any, coordinator: Any,
        wait_for_turn_fn: Any = None, **kw: Any,
    ) -> RunResult:
        calls["n"] += 1
        if calls["n"] == 1:
            # Seed the minimal state the hold loop's State.load reads.
            State(id=ch.id, step="brainstorm", step_attempt=0,
                  status="running").save(ch.state_dir)
            coordinator.set_pending_model("anthropic:claude-fable-5")
            await coordinator.signal_pause(reason="model-override", via="frame")
            return RunResult.PAUSED
        return RunResult.COMPLETED

    # A wrong impl (treating the model-override pause as a ctrl-c hold) would emit
    # _RESUME_PROMPT and block on user_chat_input_fn — with no client to answer,
    # that blocks forever, so the deadline concretely enforces the no-prompt path.
    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(
            quest, run_fn=_run_fn, hibernate=False)

    assert result is RunResult.COMPLETED
    assert calls["n"] == 2                       # re-entered exactly once
    assert applied == [("anthropic:claude-fable-5", "brainstorm")]


@pytest.mark.anyio
async def test_model_override_pause_with_empty_arg_resumes_without_applying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.remote.session.daemon as daemon_mod

    quest = _make_change(tmp_path, "2026-06-15-emp")

    applied: list[tuple[str, str]] = []
    monkeypatch.setattr(
        daemon_mod, "apply_model_override",
        lambda ch, state, arg, *, step_name: applied.append((arg, step_name)))

    calls = {"n": 0}

    async def _run_fn(
        ch: Quest, *, on_event: Any, user_chat_input_fn: Any, coordinator: Any,
        wait_for_turn_fn: Any = None, **kw: Any,
    ) -> RunResult:
        calls["n"] += 1
        if calls["n"] == 1:
            State(id=ch.id, step="brainstorm", step_attempt=0,
                  status="running").save(ch.state_dir)
            coordinator.set_pending_model("")
            await coordinator.signal_pause(reason="model-override", via="frame")
            return RunResult.PAUSED
        return RunResult.COMPLETED

    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(
            quest, run_fn=_run_fn, hibernate=False)

    assert result is RunResult.COMPLETED
    assert calls["n"] == 2
    assert applied == []


@pytest.mark.anyio
async def test_serve_threads_opening_model_to_run_fn(tmp_path) -> None:
    from imp.engine.executor import RunResult
    from imp.remote.session.daemon import serve_quest_session

    captured: dict[str, object] = {}

    async def _run_fn(quest, **kw):
        captured.update(kw)
        return RunResult.COMPLETED

    await serve_quest_session(
        _make_change(tmp_path, "2026-07-10-mdl"), run_fn=_run_fn, hibernate=False,
        model="anthropic:claude-fable-5")
    assert captured["opening_model"] == "anthropic:claude-fable-5"


def test_main_forwards_model_to_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import functools

    import imp.remote.session.daemon as daemon

    loaded = SimpleNamespace(quest_dir=tmp_path)
    captured: dict[str, Any] = {}

    def fake_load(control_root: Path, quest_id: str) -> object:
        return loaded

    def fake_run(fn: Any) -> None:
        captured["fn"] = fn

    monkeypatch.setattr(daemon.Quest, "load", staticmethod(fake_load))
    monkeypatch.setattr(daemon.anyio, "run", fake_run)

    rc = daemon.main([
        "--control-root", str(tmp_path),
        "--quest-id", "2026-07-10-mdl",
        "--model", "a:b",
    ])

    assert rc == 0
    fn = captured["fn"]
    assert isinstance(fn, functools.partial)
    assert fn.func is daemon.serve_quest_session
    assert fn.keywords.get("model") == "a:b"


@pytest.mark.anyio
async def test_resident_save_forwards_the_leave_reason(tmp_path: Path) -> None:
    quest = _make_change(tmp_path, "2026-06-15-svr")
    sock = str(session_socket_path(quest.control_root, quest.id))
    saved: list[tuple[Quest, str | None]] = []

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> None:
        await anyio.sleep_forever()   # only a save-cancel ends this run

    async with anyio.create_task_group() as tg:
        async def _serve() -> None:
            await serve_quest_session(
                quest, run_fn=fake_run, hibernate=False,
                save_left=lambda ch, reason: saved.append((ch, reason)))

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        client = SessionClient(sock, quest.id, quest.title,
                               on_event=lambda _e: None)
        await client.connect()
        await client.send_control(CONTROL_SAVE, "ctrl-d")
        await wait_until(lambda: saved)
        assert saved == [(quest, "ctrl-d")]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_script_step_hold_shows_rerun_copy_and_refuses_redirect(
    tmp_path: Path,
) -> None:
    # A bare _make_change quest resolves its workflow to the bundled `imp`
    # workflow (parse_default_workflow -> "imp"), where "verify" is a script step.
    quest = _make_change(tmp_path, "2026-06-15-scr")
    _seed_step(quest, "verify")
    sock = str(session_socket_path(quest.control_root, quest.id))
    calls = {"n": 0}
    drained: dict[str, Any] = {}

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        calls["n"] += 1
        if calls["n"] == 1:
            await coordinator.signal_pause(reason="ctrl-c")
            return RunResult.PAUSED
        drained["v"] = coordinator.drain_interject()
        return RunResult.COMPLETED

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_quest_session(
                quest, run_fn=fake_run, hibernate=False)

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        received: list[dict[str, Any]] = []
        client = SessionClient(sock, quest.id, quest.title,
                               on_event=received.append)
        await client.connect()
        tg.start_soon(client.receive_events)

        def _awaiting() -> list[dict[str, Any]]:
            return [e for e in received
                    if e.get("type") == "awaiting_user_input"]

        await wait_until(lambda: _awaiting())
        # (a) the script hold copy — re-run/detach, no redirect offer
        assert _awaiting()[0]["payload"]["steer_hint"] == (
            "paused · enter to re-run the gate · Ctrl-D to leave")

        # (b) a typed message is refused with a plain acknowledgement, not dropped
        await client.send_input("go left")
        await wait_until(lambda: any(e.get("type") == "script_output" for e in received))
        refusal = next(e for e in received if e.get("type") == "script_output")
        assert refusal["payload"]["stdout"] == (
            "a verify gate takes no redirect; press enter to re-run or Ctrl-D to leave")

        # (c) stayed at the hold: re-prompted and the gate did NOT re-run
        await wait_until(lambda: len(_awaiting()) >= 2)
        assert calls["n"] == 1

        # press enter (empty line) -> re-run the gate to completion
        await client.send_input("")
        await wait_until(lambda: "r" in result)
        assert result["r"] is RunResult.COMPLETED
        assert calls["n"] == 2
        assert drained["v"] is None   # the refused message was never enqueued
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_agent_step_hold_keeps_redirect_copy_and_consumes_message(
    tmp_path: Path,
) -> None:
    # "task-lead" is an agent step in the bundled `imp` workflow.
    quest = _make_change(tmp_path, "2026-06-15-agt")
    _seed_step(quest, "task-lead")
    sock = str(session_socket_path(quest.control_root, quest.id))
    calls = {"n": 0}
    drained: dict[str, Any] = {}

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        calls["n"] += 1
        if calls["n"] == 1:
            await coordinator.signal_pause(reason="ctrl-c")
            return RunResult.PAUSED
        drained["v"] = coordinator.drain_interject()
        return RunResult.COMPLETED

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_quest_session(
                quest, run_fn=fake_run, hibernate=False)

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        received: list[dict[str, Any]] = []
        client = SessionClient(sock, quest.id, quest.title,
                               on_event=received.append)
        await client.connect()
        tg.start_soon(client.receive_events)

        def _awaiting() -> list[dict[str, Any]]:
            return [e for e in received
                    if e.get("type") == "awaiting_user_input"]

        await wait_until(lambda: _awaiting())
        # unchanged agent hold copy — offers a redirect
        assert _awaiting()[0]["payload"]["steer_hint"] == (
            "paused · enter to continue · type to redirect · Ctrl-D to leave")

        await client.send_input("go left")
        await wait_until(lambda: "r" in result)
        assert result["r"] is RunResult.COMPLETED
        assert calls["n"] == 2
        assert drained["v"] == "go left"   # consumed as an interject, as today
        assert not any(e.get("type") == "script_output" for e in received)
        await client.aclose()
        tg.cancel_scope.cancel()


def test_resolve_step_kind_maps_step_names_to_kinds(tmp_path: Path) -> None:
    import imp.remote.session.daemon as daemon

    # Bare quest resolves to the bundled `imp` workflow: "verify" is a verify-gate
    # script step, "task-lead" is an agent step, and an unset step is None.
    quest = _make_change(tmp_path, "2026-06-15-rsk")
    st = State.load(quest.state_dir)

    st.step = "verify"
    st.save(quest.state_dir)
    assert daemon._resolve_step_kind(quest) == "script"

    st.step = "task-lead"
    st.save(quest.state_dir)
    assert daemon._resolve_step_kind(quest) == "agent"

    st.step = None
    st.save(quest.state_dir)
    assert daemon._resolve_step_kind(quest) is None

    st.step = "not-a-real-step"
    st.save(quest.state_dir)
    assert daemon._resolve_step_kind(quest) is None



@pytest.mark.anyio
async def test_sandbox_control_no_pause_single_dispatch(tmp_path: Path) -> None:
    """CONTROL_SET_SANDBOX sets coordinator.unsandboxed but must NOT call signal_pause.

    Old behavior: the server called signal_pause("sandbox-override"), the daemon
    detected it, persisted state, and re-dispatched (calls["n"] == 2).
    New behavior: no signal_pause at all — the flag is live-only; the run
    continues to completion in a single dispatch (calls["n"] == 1).
    """
    from imp.remote.session.protocol import CONTROL_SET_SANDBOX

    quest = _make_change(tmp_path, "2026-06-15-sbx")
    sock = str(session_socket_path(quest.control_root, quest.id))
    run_ready = anyio.Event()
    seen: dict[str, Any] = {}

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        run_ready.set()
        await wait_until(lambda: coordinator.unsandboxed)
        seen["should_pause"] = coordinator.should_pause
        return RunResult.COMPLETED

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_quest_session(
                quest, run_fn=fake_run, hibernate=False)

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        client = SessionClient(sock, quest.id, quest.title,
                               on_event=lambda _e: None)
        await client.connect()
        with anyio.fail_after(30):   # deadlock backstop only
            await run_ready.wait()
        await client.send_control(CONTROL_SET_SANDBOX, "off")
        await wait_until(lambda: "r" in result)
        assert result["r"] is RunResult.COMPLETED
        assert seen["should_pause"] is False    # no signal_pause was issued
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_resume_restores_unsandboxed_into_coordinator(tmp_path: Path) -> None:
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    st = State.load(quest.state_dir)
    st.status = "paused"
    st.unsandboxed = True
    st.save(quest.state_dir)

    seen: dict[str, object] = {}

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any, coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> RunResult:
        seen["unsandboxed"] = coordinator.unsandboxed
        return RunResult.COMPLETED

    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(
            quest, run_fn=fake_run, hibernate=False, resume=True)

    assert result is RunResult.COMPLETED
    assert seen["unsandboxed"] is True     # restored from State into a fresh coordinator


def test_is_side_effect_pause_fires_for_reason_in_set() -> None:
    from imp.remote.session.daemon import _is_side_effect_pause

    coord = SimpleNamespace(should_pause=True, pause_reason="model-override")
    assert _is_side_effect_pause(RunResult.PAUSED, coord) is True


def test_is_side_effect_pause_false_for_plain_interrupt_reason() -> None:
    from imp.remote.session.daemon import _is_side_effect_pause

    coord = SimpleNamespace(should_pause=True, pause_reason="ctrl-c")
    assert _is_side_effect_pause(RunResult.PAUSED, coord) is False


def test_is_side_effect_pause_false_when_result_not_paused() -> None:
    from imp.remote.session.daemon import _is_side_effect_pause

    coord = SimpleNamespace(should_pause=True, pause_reason="model-override")
    assert _is_side_effect_pause(RunResult.COMPLETED, coord) is False


def test_hold_prompt_suppressed_for_side_effect_reason() -> None:
    from imp.remote.session.daemon import _hold_prompt

    coord = SimpleNamespace(should_pause=True, pause_reason="model-override")
    assert _hold_prompt(RunResult.PAUSED, coord) is None


def test_hold_prompt_returns_resume_for_plain_interrupt() -> None:
    from imp.remote.session.daemon import _RESUME_PROMPT, _hold_prompt

    coord = SimpleNamespace(should_pause=True, pause_reason="ctrl-c")
    assert _hold_prompt(RunResult.PAUSED, coord) == _RESUME_PROMPT


def test_run_compaction_slims_transcript_and_emits_confirmation(
    tmp_path: Path,
) -> None:
    from imp.agents.compaction import RESULT_PLACEHOLDER
    from imp.agents.runner import Message, ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript
    from imp.engine.state import CurrentStep, State
    from imp.remote.session.daemon import _run_compaction

    quest = _make_change(tmp_path, "2026-06-15-cmp")
    tdir = tmp_path / "steps" / "brainstorm"
    tdir.mkdir(parents=True)
    t = Transcript(tdir / "transcript.jsonl")
    t.append(Message(role="assistant", content=(
        ToolUseBlock(id="tu1", name="read_file", input={"path": "a"}),)))
    t.append(Message(role="user", content=(
        ToolResultBlock(tool_use_id="tu1", content="BIG BODY"),)))
    State(id=quest.id, step="brainstorm", step_attempt=0, status="running",
          current_step=CurrentStep(transcript_dir=tdir)).save(quest.state_dir)

    events: list[dict[str, Any]] = []
    _run_compaction(quest, events.append)

    results = [b for m in t.load() for b in m.content
               if isinstance(b, ToolResultBlock)]
    assert results and all(b.content == RESULT_PLACEHOLDER for b in results)
    assert events == [{"type": "script_output",
                       "payload": {"stdout": "compacted — omitted 1 tool results"}}]


@pytest.mark.anyio
async def test_compact_pause_reenters_after_running_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.remote.session.daemon as daemon_mod

    quest = _make_change(tmp_path, "2026-06-15-cmp2")

    ran: list[str] = []
    monkeypatch.setattr(
        daemon_mod, "_run_compaction",
        lambda ch, on_event: ran.append(ch.id))

    calls = {"n": 0}

    async def _run_fn(
        ch: Quest, *, on_event: Any, user_chat_input_fn: Any, coordinator: Any,
        wait_for_turn_fn: Any = None, **kw: Any,
    ) -> RunResult:
        calls["n"] += 1
        if calls["n"] == 1:
            State(id=ch.id, step="brainstorm", step_attempt=0,
                  status="running").save(ch.state_dir)
            await coordinator.signal_pause(reason="compact", via="frame")
            return RunResult.PAUSED
        return RunResult.COMPLETED

    with anyio.fail_after(30):   # deadlock backstop only
        result = await serve_quest_session(
            quest, run_fn=_run_fn, hibernate=False)

    assert result is RunResult.COMPLETED
    assert calls["n"] == 2
    assert ran == [quest.id]


def test_config_pause_error_passes_a_bare_load_error() -> None:
    """A bare (un-grouped) LoadError has no crash sibling, so it is returned
    unchanged and renders the clean config pause (AC 3)."""
    from imp.engine.errors import LoadError
    from imp.remote.session.daemon import _config_pause_error

    exc = LoadError("bad yaml")
    assert _config_pause_error(exc) is exc


def test_config_pause_error_unwraps_real_anyio_group() -> None:
    """A LoadError raised inside an anyio task group surfaces as a
    BaseExceptionGroup (possibly nested); with only load + cancellation leaves,
    _config_pause_error yields the load error for the clean pause (AC 2)."""
    from imp.engine.errors import LoadError
    from imp.remote.session.daemon import _config_pause_error

    async def _boom() -> None:
        async with anyio.create_task_group() as outer:
            async def _inner() -> None:
                async with anyio.create_task_group() as inner:
                    async def _raise() -> None:
                        raise LoadError("phases: required key missing")
                    inner.start_soon(_raise)
            outer.start_soon(_inner)

    with pytest.raises(BaseExceptionGroup) as ei:
        anyio.run(_boom)
    leaf = _config_pause_error(ei.value)
    assert isinstance(leaf, LoadError)
    assert leaf.message == "phases: required key missing"


def test_config_pause_error_returns_none_for_a_non_load_group() -> None:
    """A group carrying only a non-LoadError crash yields None, so main
    re-raises it unchanged (AC 3: the crash keeps its traceback)."""
    from imp.remote.session.daemon import _config_pause_error

    async def _boom() -> None:
        async with anyio.create_task_group() as tg:
            async def _raise() -> None:
                raise ValueError("kaboom")
            tg.start_soon(_raise)

    with pytest.raises(BaseExceptionGroup) as ei:
        anyio.run(_boom)
    assert _config_pause_error(ei.value) is None


def test_config_pause_error_returns_none_for_a_mixed_group() -> None:
    """AC 1: a group with both a LoadError leaf and a genuine non-cancellation
    crash returns None, so main re-raises and the crash's traceback survives.
    The construction is order-independent: the crash must win regardless."""
    from imp.engine.errors import LoadError
    from imp.remote.session.daemon import _config_pause_error

    load_first = BaseExceptionGroup(
        "eg", [LoadError("phases: required key missing"), RuntimeError("boom")])
    crash_first = BaseExceptionGroup(
        "eg", [RuntimeError("boom"), LoadError("phases: required key missing")])
    assert _config_pause_error(load_first) is None
    assert _config_pause_error(crash_first) is None


def test_config_pause_error_ignores_cancellation_siblings() -> None:
    """AC 2: a group whose only genuine leaf is a LoadError, accompanied by a
    cancellation sibling, still yields the LoadError for the clean pause. The
    cancellation leaf is constructed directly because a real anyio task group
    swallows the cancelled sibling and yields only `[LoadError]`, so it cannot
    exercise this branch."""
    import asyncio

    from imp.engine.errors import LoadError
    from imp.remote.session.daemon import _config_pause_error

    group = BaseExceptionGroup(
        "eg", [LoadError("phases: required key missing"), asyncio.CancelledError()])
    leaf = _config_pause_error(group)
    assert isinstance(leaf, LoadError)
    assert leaf.message == "phases: required key missing"


def test_main_records_config_error_pause_on_wrapped_load_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 4a: a load failure that reaches main() wrapped in a BaseExceptionGroup
    is recorded as a config-classified error pause (error + workflow_paused
    reason='config') so the foreground's _pause_reason classifies it as a
    `config error`; the quest is left cleanly paused and main exits 2, with no
    traceback printed."""
    import imp.remote.session.daemon as daemon
    from imp.cli.main import _pause_reason
    from imp.engine.errors import LoadError
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    monkeypatch.setattr(daemon.Quest, "load", staticmethod(lambda r, c: quest))

    def _raise(*_a: object, **_k: object) -> object:
        raise BaseExceptionGroup(
            "eg", [LoadError("phases: required key missing")])

    monkeypatch.setattr(daemon.anyio, "run", _raise)

    rc = daemon.main(["--control-root", str(tmp_path), "--quest-id", "feat"])
    assert rc == 2

    events = list(quest.event_log().read())
    err = [e for e in events if e["type"] == "error"][-1]
    assert err["payload"]["category"] == "config"
    assert "phases: required key missing" in err["payload"]["message"]
    paused = [e for e in events if e["type"] == "workflow_paused"][-1]
    assert paused["payload"]["reason"] == "config"

    info = _pause_reason(events)
    assert info.base == "config"
    assert "phases: required key missing" in info.cause

    assert State.load(quest.state_dir).status == "paused"
    assert "Traceback (most recent call last):" not in capsys.readouterr().err


def test_steer_copy_is_verbatim_and_shared() -> None:
    from imp.engine.completion import INTERRUPT_STEER_HINT, steer_hint
    from imp.remote.session.daemon import (
        _RESUME_PROMPT,
        _SCRIPT_HOLD_PROMPT,
        _SCRIPT_REDIRECT_REFUSED,
    )

    assert steer_hint("interrupted") == (
        "interrupted · enter to continue · type to redirect · Ctrl-D to leave")
    assert steer_hint("paused") == (
        "paused · enter to continue · type to redirect · Ctrl-D to leave")
    # The two agent prompts are built from the one shared constructor.
    assert steer_hint("interrupted") == INTERRUPT_STEER_HINT
    assert steer_hint("paused") == _RESUME_PROMPT
    # Script surfaces name Ctrl-D, not "detach".
    assert _SCRIPT_HOLD_PROMPT == (
        "paused · enter to re-run the gate · Ctrl-D to leave")
    assert _SCRIPT_REDIRECT_REFUSED == (
        "a verify gate takes no redirect; press enter to re-run or Ctrl-D to leave")


@pytest.mark.anyio
async def test_agent_step_hold_empty_reply_continues_without_interject(
    tmp_path: Path,
) -> None:
    quest = _make_change(tmp_path, "2026-06-15-agt-empty")
    _seed_step(quest, "task-lead")
    sock = str(session_socket_path(quest.control_root, quest.id))
    calls = {"n": 0}
    drained: dict[str, Any] = {}

    async def fake_run(
        quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        calls["n"] += 1
        if calls["n"] == 1:
            await coordinator.signal_pause(reason="ctrl-c")
            return RunResult.PAUSED
        drained["v"] = coordinator.drain_interject()
        return RunResult.COMPLETED

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_quest_session(
                quest, run_fn=fake_run, hibernate=False)

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        received: list[dict[str, Any]] = []
        client = SessionClient(sock, quest.id, quest.title,
                               on_event=received.append)
        await client.connect()
        tg.start_soon(client.receive_events)
        await wait_until(
            lambda: any(e.get("type") == "awaiting_user_input" for e in received))
        await client.send_input("")       # empty Enter → continue
        await wait_until(lambda: "r" in result)
        assert result["r"] is RunResult.COMPLETED
        assert calls["n"] == 2
        assert drained["v"] is None        # empty was never enqueued as an interject
        await client.aclose()
        tg.cancel_scope.cancel()
