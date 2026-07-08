from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from imp.engine.change import Change
from imp.engine.executor import RunResult
from imp.engine.state import State
from imp.remote.session.client import SessionClient
from imp.remote.session.daemon import _is_interrupt_pause, serve_change_session
from imp.remote.session.discover import session_socket_path
from imp.remote.session.protocol import (
    CONTROL_ABANDON,
    CONTROL_ARCHIVE,
    CONTROL_DISCARD,
    CONTROL_SAVE,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_change(tmp_path: Path, change_id: str) -> Change:
    change = Change(
        id=change_id,
        title="T",
        base_branch="main",
        control_root=tmp_path,
        project_root=tmp_path,
        change_dir=tmp_path / ".imp" / "change",
    )
    State(id=change_id, step=None, step_attempt=0, status="paused").save(
        change.state_dir)
    return change


async def _wait_for_socket(sock: str) -> None:
    with anyio.fail_after(5):
        while not Path(sock).exists():
            await anyio.sleep(0.02)


@pytest.mark.anyio
async def test_serves_on_per_change_socket_and_bridges_run_change_seams(
    tmp_path: Path,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-abc")
    sock = str(session_socket_path(change.control_root, change.id))

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> str:
        reply = await user_chat_input_fn("")
        on_event({"type": "agent_output", "payload": {"reply": reply}})
        return reply

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_change_session(
                change, run_fn=fake_run, hibernate=False)

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        received: list[dict[str, Any]] = []
        client = SessionClient(sock, change.id, change.title,
                               on_event=received.append)
        await client.connect()
        tg.start_soon(client.receive_events)
        await anyio.sleep(0.05)  # let the gate open (next_input is awaiting)
        await client.send_input("ship it")
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "ship it"
        with anyio.fail_after(5):
            while not received:
                await anyio.sleep(0.02)
        assert received == [
            {"type": "agent_output", "payload": {"reply": "ship it"}}]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_zero_client_gate_hibernates_by_default(tmp_path: Path) -> None:
    from imp.engine.awaiting_user_input import AwaitingUserInput

    change = _make_change(tmp_path, "2026-06-15-def")

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> str:
        try:
            await user_chat_input_fn("")
        except AwaitingUserInput:
            return "hibernated"
        return "resident"

    with anyio.fail_after(5):
        result = await serve_change_session(
            change, run_fn=fake_run, first_attach_timeout=0.1)
    assert result == "hibernated"


@pytest.mark.anyio
async def test_lock_is_held_during_run_and_released_after(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-ghi")
    lock_path = change.change_dir / "lock"
    release = anyio.Event()

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> str:
        await release.wait()
        return "done"

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_change_session(
                change, run_fn=fake_run, hibernate=False)

        tg.start_soon(_serve)
        await anyio.sleep(0.05)  # let serve acquire the lock and enter run_fn
        assert lock_path.exists()  # ProjectLock writes the holder-pid file
        release.set()
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "done"
        assert not lock_path.exists()  # released on serve() return
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_interrupt_control_pauses_the_shared_coordinator(
    tmp_path: Path,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-jkl")
    sock = str(session_socket_path(change.control_root, change.id))

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> str:
        with anyio.fail_after(5):
            while not coordinator.should_pause:
                await anyio.sleep(0.02)
        return "paused"

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_change_session(
                change, run_fn=fake_run, hibernate=False)

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        client = SessionClient(sock, change.id, change.title,
                               on_event=lambda _e: None)
        await client.connect()
        await client.send_control("interrupt")
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] == "paused"
        await client.aclose()
        tg.cancel_scope.cancel()


def test_main_loads_named_change_and_serves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import imp.remote.session.daemon as daemon

    loaded = object()
    calls: dict[str, Any] = {}

    def fake_load(control_root: Path, change_id: str) -> object:
        calls["load"] = (control_root, change_id)
        return loaded

    def fake_run(fn: Any, arg: Any) -> None:
        calls["run"] = (fn, arg)

    monkeypatch.setattr(daemon.Change, "load", staticmethod(fake_load))
    monkeypatch.setattr(daemon.anyio, "run", fake_run)

    rc = daemon.main(
        ["--control-root", str(tmp_path), "--change-id", "2026-06-15-abc"])

    assert rc == 0
    assert calls["load"] == (tmp_path, "2026-06-15-abc")
    assert calls["run"] == (daemon.serve_change_session, loaded)


@pytest.mark.anyio
async def test_resident_abandon_runs_teardown(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-aba")
    sock = str(session_socket_path(change.control_root, change.id))
    torn: list[Change] = []

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> str:
        await anyio.sleep_forever()   # only an abandon-cancel ends this run
        return "x"

    async with anyio.create_task_group() as tg:
        async def _serve() -> None:
            await serve_change_session(
                change, run_fn=fake_run, hibernate=False,
                tear_down=lambda ch: torn.append(ch))

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        client = SessionClient(sock, change.id, change.title,
                               on_event=lambda _e: None)
        await client.connect()
        await client.send_control(CONTROL_ABANDON)
        with anyio.fail_after(5):
            while not torn:
                await anyio.sleep(0.02)
        assert torn == [change]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_interrupt_resume_prompt_not_duplicated_on_existing_client(
    tmp_path: Path,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-dup")
    sock = str(session_socket_path(change.control_root, change.id))
    calls = {"n": 0}

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
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
            result["r"] = await serve_change_session(
                change, run_fn=fake_run, hibernate=False)

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        seen1: list[dict[str, Any]] = []
        c1 = SessionClient(sock, change.id, change.title, on_event=seen1.append)
        await c1.connect()
        tg.start_soon(c1.receive_events)
        with anyio.fail_after(5):
            while sum(e.get("type") == "awaiting_user_input" for e in seen1) < 1:
                await anyio.sleep(0.02)
        # Late client attaches at the resume gate.
        seen2: list[dict[str, Any]] = []
        c2 = SessionClient(sock, change.id, change.title, on_event=seen2.append)
        await c2.connect()
        tg.start_soon(c2.receive_events)
        with anyio.fail_after(5):
            while sum(e.get("type") == "awaiting_user_input" for e in seen2) < 1:
                await anyio.sleep(0.02)
        await anyio.sleep(0.1)
        assert sum(e.get("type") == "awaiting_user_input" for e in seen1) == 1
        await c2.send_input("go")
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
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
    change = _make_change(tmp_path, "2026-06-15-rzm")
    sock = str(session_socket_path(change.control_root, change.id))
    calls = {"n": 0}

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> RunResult:
        calls["n"] += 1
        if calls["n"] == 1:
            await coordinator.signal_pause(reason="ctrl-c")
            return RunResult.PAUSED
        assert not coordinator.should_pause  # reset() must have cleared this
        on_event({"type": "agent_output",
                  "payload": {"redirect": coordinator.drain_interject()}})
        return RunResult.COMPLETED

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_change_session(
                change, run_fn=fake_run, hibernate=False)

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        received: list[dict[str, Any]] = []
        client = SessionClient(sock, change.id, change.title,
                               on_event=received.append)
        await client.connect()
        tg.start_soon(client.receive_events)
        with anyio.fail_after(5):
            while not any(
                    e.get("type") == "awaiting_user_input" for e in received):
                await anyio.sleep(0.02)
        await client.send_input("go left")
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] is RunResult.COMPLETED
        assert calls["n"] == 2
        assert {"type": "agent_output",
                "payload": {"redirect": "go left"}} in received
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_interrupt_pause_hibernates_with_zero_clients(
    tmp_path: Path,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-rzh")
    calls = {"n": 0}

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> RunResult:
        calls["n"] += 1
        await coordinator.signal_pause(reason="ctrl-c")
        return RunResult.PAUSED

    with anyio.fail_after(5):
        result = await serve_change_session(
            change, run_fn=fake_run, hibernate=True, first_attach_timeout=0.1)
    assert result is RunResult.PAUSED
    assert calls["n"] == 1


@pytest.mark.anyio
async def test_resume_journals_workflow_resumed_without_flipping_status(
    tmp_path: Path,
) -> None:
    from imp.engine.state import State
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    change = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    st = State.load(change.state_dir)
    st.status = "paused"
    st.save(change.state_dir)

    seen: dict[str, str] = {}

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> RunResult:
        seen["status"] = State.load(change.state_dir).status
        return RunResult.COMPLETED

    with anyio.fail_after(5):
        result = await serve_change_session(
            change, run_fn=fake_run, hibernate=False, resume=True)

    assert result is RunResult.COMPLETED
    assert seen["status"] == "paused"           # never flipped to a persisted "running"
    assert change.events_path.exists() and "workflow_resumed" in change.events_path.read_text()


@pytest.mark.anyio
async def test_resume_sets_resume_pending_for_a_stale_lock_crash(tmp_path: Path) -> None:
    """A hard-killed run leaves a lock file naming a now-dead pid (the OS frees
    the flock but not the pid file). The pre-acquire snapshot reads that as a
    crash and sets resume_pending to the current step so the executor replays it."""
    import subprocess

    from imp.engine.state import State
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    change = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    st = State.load(change.state_dir)
    st.step = "review"
    st.status = "paused"
    st.resume_pending = None
    st.save(change.state_dir)
    p = subprocess.Popen(["true"])           # dead pid ⇒ stale lock ⇒ crash
    p.wait()
    (change.change_dir / "lock").write_text(str(p.pid))

    seen: dict[str, object] = {}

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> RunResult:
        seen["resume_pending"] = State.load(change.state_dir).resume_pending
        return RunResult.COMPLETED

    with anyio.fail_after(5):
        result = await serve_change_session(
            change, run_fn=fake_run, hibernate=False, resume=True)

    assert result is RunResult.COMPLETED
    assert seen["resume_pending"] == "review"


@pytest.mark.anyio
async def test_resume_with_no_lock_does_not_set_resume_pending(tmp_path: Path) -> None:
    """A clean pause leaves no lock file: resume must NOT replay (resume_pending
    stays unset) so the run advances normally."""
    from imp.engine.state import State
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    change = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    st = State.load(change.state_dir)
    st.step = "review"
    st.status = "paused"
    st.resume_pending = None
    st.save(change.state_dir)

    seen: dict[str, object] = {}

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> RunResult:
        seen["resume_pending"] = State.load(change.state_dir).resume_pending
        return RunResult.COMPLETED

    with anyio.fail_after(5):
        result = await serve_change_session(
            change, run_fn=fake_run, hibernate=False, resume=True)

    assert result is RunResult.COMPLETED
    assert seen["resume_pending"] is None


@pytest.mark.anyio
async def test_default_spawn_does_no_resume_prep(tmp_path: Path) -> None:
    from imp.engine.state import State
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    change = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    st = State.load(change.state_dir)
    st.status = "paused"
    st.save(change.state_dir)

    seen: dict[str, str] = {}

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> RunResult:
        seen["status"] = State.load(change.state_dir).status
        return RunResult.COMPLETED

    with anyio.fail_after(5):
        result = await serve_change_session(
            change, run_fn=fake_run, hibernate=False)  # resume defaults False

    assert result is RunResult.COMPLETED
    assert seen["status"] == "paused"           # untouched: no resume prep ran
    assert not change.events_path.exists() or (
        "workflow_resumed" not in change.events_path.read_text())


def test_main_forwards_resume_flag_via_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import functools

    import imp.remote.session.daemon as daemon

    loaded = object()
    captured: dict[str, Any] = {}

    def fake_load(control_root: Path, change_id: str) -> object:
        return loaded

    def fake_run(fn: Any) -> None:   # partial form takes a single positional
        captured["fn"] = fn

    monkeypatch.setattr(daemon.Change, "load", staticmethod(fake_load))
    monkeypatch.setattr(daemon.anyio, "run", fake_run)

    rc = daemon.main([
        "--control-root", str(tmp_path),
        "--change-id", "2026-06-15-abc",
        "--resume",
    ])

    assert rc == 0
    fn = captured["fn"]
    assert isinstance(fn, functools.partial)
    assert fn.func is daemon.serve_change_session
    assert fn.args == (loaded,)
    assert fn.keywords == {"resume": True}


def test_append_workflow_resumed_is_idempotent_when_trailing(tmp_path: Path) -> None:
    import imp.remote.session.daemon as daemon
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    change = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    log = change.event_log()
    daemon._append_workflow_resumed(log)
    daemon._append_workflow_resumed(log)   # trailing already resumed → no-op
    assert [e["type"] for e in log.read()].count("workflow_resumed") == 1


@pytest.mark.anyio
async def test_two_interrupt_rounds_leave_no_open_pause(tmp_path: Path) -> None:
    from imp.cli.signals import _open_pause_exists

    change = _make_change(tmp_path, "2026-06-15-bal")
    calls = {"n": 0}

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> RunResult:
        calls["n"] += 1
        if calls["n"] <= 2:
            change.event_log().append(
                agent_id=None, type="workflow_paused",
                payload={"reason": "interrupt"})
            await coordinator.signal_pause(reason="ctrl-c")
            return RunResult.PAUSED
        return RunResult.COMPLETED

    sock = str(session_socket_path(change.control_root, change.id))
    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_change_session(
                change, run_fn=fake_run, hibernate=False)

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        seen: list[dict[str, Any]] = []
        client = SessionClient(sock, change.id, change.title, on_event=seen.append)
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
        with anyio.fail_after(10):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] is RunResult.COMPLETED
        types = [e["type"] for e in change.event_log().read()]
        assert types.count("workflow_paused") == 2
        assert types.count("workflow_resumed") == 2
        assert not _open_pause_exists(change)
        await client.aclose()
        tg.cancel_scope.cancel()



def test_main_unknown_change_id_prints_clean_error_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.remote.session.daemon as daemon
    from imp.engine.errors import LoadError

    def boom(_root: Path, _cid: str) -> object:
        raise LoadError("no such change feat")

    monkeypatch.setattr(daemon.Change, "load", staticmethod(boom))
    rc = daemon.main(["--control-root", str(tmp_path), "--change-id", "feat"])
    assert rc == 2
    assert "no such change feat" in capsys.readouterr().err


def test_main_maps_paused_outcome_to_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.remote.session.daemon as daemon
    from imp.engine.executor import RunResult

    monkeypatch.setattr(daemon.Change, "load", staticmethod(lambda r, c: object()))
    monkeypatch.setattr(daemon.anyio, "run", lambda *a, **k: RunResult.PAUSED)
    rc = daemon.main(["--control-root", str(tmp_path), "--change-id", "feat"])
    assert rc == 1


def test_main_maps_paused_outcome_to_nonzero_with_kwargs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.remote.session.daemon as daemon
    from imp.engine.executor import RunResult

    monkeypatch.setattr(daemon.Change, "load", staticmethod(lambda r, c: object()))
    monkeypatch.setattr(daemon.anyio, "run", lambda *a, **k: RunResult.PAUSED)
    rc = daemon.main(["--control-root", str(tmp_path), "--change-id", "feat", "--resume"])
    assert rc == 1



def test_append_workflow_resumed_appends_after_a_real_event(tmp_path: Path) -> None:
    import imp.remote.session.daemon as daemon
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    change = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    log = change.event_log()
    daemon._append_workflow_resumed(log)
    log.append(agent_id=None, type="workflow_paused", payload={"reason": "interrupt"})
    daemon._append_workflow_resumed(log)   # real event between → a second marker
    assert [e["type"] for e in log.read()].count("workflow_resumed") == 2


def test_last_pause_was_awaiting_turn_true_for_awaiting_turn(tmp_path: Path) -> None:
    import imp.remote.session.daemon as daemon
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    change = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    change.event_log().append(
        agent_id=None, type="workflow_paused", payload={"reason": "awaiting_turn"})
    assert daemon._last_pause_was_awaiting_turn(change.event_log()) is True


def test_last_pause_was_awaiting_turn_false_for_other_reason(tmp_path: Path) -> None:
    import imp.remote.session.daemon as daemon
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    change = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    change.event_log().append(
        agent_id=None, type="workflow_paused", payload={"reason": "interrupt"})
    assert daemon._last_pause_was_awaiting_turn(change.event_log()) is False


def test_last_pause_was_awaiting_turn_false_after_resume(tmp_path: Path) -> None:
    import imp.remote.session.daemon as daemon
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    change = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    log = change.event_log()
    log.append(agent_id=None, type="workflow_paused", payload={"reason": "awaiting_turn"})
    log.append(agent_id=None, type="workflow_resumed", payload={})
    assert daemon._last_pause_was_awaiting_turn(log) is False   # pause is closed



def test_last_pause_was_awaiting_turn_false_with_no_events(tmp_path: Path) -> None:
    import imp.remote.session.daemon as daemon
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    change = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    # No events at all — the fallback must be False
    assert daemon._last_pause_was_awaiting_turn(change.event_log()) is False


@pytest.mark.anyio
async def test_resident_abandon_of_unmaterialized_draft_archives_not_tears_down(
    tmp_path: Path,
) -> None:
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    change = Change.create_draft(
        tmp_path, title="x", change_id="draft1", base_branch="main")
    assert change.materialized is False
    sock = str(session_socket_path(change.control_root, change.id))
    torn: list[Change] = []
    archived: list[Change] = []

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> str:
        await anyio.sleep_forever()   # only an abandon-cancel ends this run
        return "x"

    async with anyio.create_task_group() as tg:
        async def _serve() -> None:
            await serve_change_session(
                change, run_fn=fake_run, hibernate=False,
                tear_down=lambda ch: torn.append(ch),
                archive_left=lambda ch: archived.append(ch))

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        client = SessionClient(sock, change.id, change.title,
                               on_event=lambda _e: None)
        await client.connect()
        await client.send_control(CONTROL_ABANDON)
        with anyio.fail_after(5):
            while not archived:
                await anyio.sleep(0.02)
        assert archived == [change]
        assert torn == []
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
    change = Change.create(tmp_path, title="b", change_id="bbb", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "bbb", "main")        # bbb is now the lane head

    spawned: list = []
    monkeypatch.setattr(
        attach_mod, "spawn_session_daemon",
        lambda ch, **kw: spawned.append((ch.id, kw.get("resume"))))

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        change.event_log().append(
            agent_id=None, type="workflow_paused", payload={"reason": "awaiting_turn"})
        return RunResult.PAUSED

    with anyio.fail_after(5):
        result = await serve_change_session(change, run_fn=fake_run, hibernate=False)

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
    change = Change.create(tmp_path, title="b", change_id="bbb", base_branch="main")
    mq.enqueue_if_absent(tmp_path, "bbb", "main")        # bbb IS head, yet no spawn

    spawned: list = []
    monkeypatch.setattr(
        attach_mod, "spawn_session_daemon", lambda ch, **kw: spawned.append(ch.id))

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        change.event_log().append(
            agent_id=None, type="workflow_paused", payload={"reason": "interrupt"})
        return RunResult.PAUSED

    with anyio.fail_after(5):
        result = await serve_change_session(change, run_fn=fake_run, hibernate=False)

    assert result is RunResult.PAUSED
    assert spawned == []   # a user-pause exit suppresses the handoff-repair hook


@pytest.mark.anyio
async def test_resident_save_applies_the_save_disposition(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-sav")
    sock = str(session_socket_path(change.control_root, change.id))
    saved: list[Change] = []
    torn: list[Change] = []

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> None:
        await anyio.sleep_forever()   # only a save-cancel ends this run

    async with anyio.create_task_group() as tg:
        async def _serve() -> None:
            await serve_change_session(
                change, run_fn=fake_run, hibernate=False,
                tear_down=lambda ch: torn.append(ch),
                save_left=lambda ch: saved.append(ch))

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        client = SessionClient(sock, change.id, change.title,
                               on_event=lambda _e: None)
        await client.connect()
        await client.send_control(CONTROL_SAVE)
        with anyio.fail_after(5):
            while not saved:
                await anyio.sleep(0.02)
        assert saved == [change]
        assert torn == []
        await client.aclose()
        tg.cancel_scope.cancel()



def test_hold_prompt_resume_prompt_for_interrupt() -> None:
    import imp.remote.session.daemon as daemon

    coord = SimpleNamespace(should_pause=True)
    assert daemon._hold_prompt(RunResult.PAUSED, coord) == daemon._RESUME_PROMPT


def test_hold_prompt_none_for_script_blocked() -> None:
    import imp.remote.session.daemon as daemon

    coord = SimpleNamespace(should_pause=False)
    assert daemon._hold_prompt(RunResult.PAUSED, coord) is None


def test_hold_prompt_none_for_awaiting_turn(tmp_path: Path) -> None:
    import imp.remote.session.daemon as daemon
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    change = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    change.event_log().append(
        agent_id=None, type="workflow_paused", payload={"reason": "awaiting_turn"})
    coord = SimpleNamespace(should_pause=False)
    assert daemon._hold_prompt(RunResult.PAUSED, coord) is None


@pytest.mark.anyio
async def test_script_blocked_pause_exits_terminal(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-scb")
    calls = {"n": 0}

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        calls["n"] += 1
        log = change.event_log()
        log.append(agent_id=None, type="script_blocked",
                   payload={"step": "integrate", "exit": 3, "stdout": "",
                            "stderr": "integration blocked: worktree dirty"})
        log.append(agent_id=None, type="workflow_paused",
                   payload={"reason": "script_blocked"})
        return RunResult.PAUSED

    with anyio.fail_after(5):
        result = await serve_change_session(change, run_fn=fake_run, hibernate=False)
    assert result is RunResult.PAUSED
    assert calls["n"] == 1  # exited terminal — never looped


@pytest.mark.anyio
async def test_script_blocked_pause_hibernates_with_zero_clients(
    tmp_path: Path,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-sch")
    calls = {"n": 0}

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        calls["n"] += 1
        log = change.event_log()
        log.append(agent_id=None, type="script_blocked",
                   payload={"step": "integrate", "exit": 3, "stdout": "",
                            "stderr": "blocked"})
        log.append(agent_id=None, type="workflow_paused",
                   payload={"reason": "script_blocked"})
        return RunResult.PAUSED

    with anyio.fail_after(5):
        result = await serve_change_session(
            change, run_fn=fake_run, hibernate=True, first_attach_timeout=0.1)
    assert result is RunResult.PAUSED
    assert calls["n"] == 1   # held once, then hibernated — never looped


@pytest.mark.anyio
async def test_resident_archive_applies_the_archive_disposition(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-arc")
    sock = str(session_socket_path(change.control_root, change.id))
    archived: list[Change] = []
    discarded: list[Change] = []

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> None:
        await anyio.sleep_forever()

    async with anyio.create_task_group() as tg:
        async def _serve() -> None:
            await serve_change_session(
                change, run_fn=fake_run, hibernate=False,
                archive_left=lambda ch: archived.append(ch),
                discard_left=lambda ch: discarded.append(ch))

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        client = SessionClient(sock, change.id, change.title,
                               on_event=lambda _e: None)
        await client.connect()
        await client.send_control(CONTROL_ARCHIVE)
        with anyio.fail_after(5):
            while not archived:
                await anyio.sleep(0.02)
        assert archived == [change]
        assert discarded == []
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_resident_discard_applies_the_discard_disposition(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-dis")
    sock = str(session_socket_path(change.control_root, change.id))
    archived: list[Change] = []
    discarded: list[Change] = []

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> None:
        await anyio.sleep_forever()

    async with anyio.create_task_group() as tg:
        async def _serve() -> None:
            await serve_change_session(
                change, run_fn=fake_run, hibernate=False,
                archive_left=lambda ch: archived.append(ch),
                discard_left=lambda ch: discarded.append(ch))

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        client = SessionClient(sock, change.id, change.title,
                               on_event=lambda _e: None)
        await client.connect()
        await client.send_control(CONTROL_DISCARD)
        with anyio.fail_after(5):
            while not discarded:
                await anyio.sleep(0.02)
        assert discarded == [change]
        assert archived == []
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_serve_forwards_non_interactive_to_run_change(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-nint")
    captured: dict[str, Any] = {}

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
        headless: bool = False,
    ) -> str:
        captured["headless"] = headless
        return "done"

    with anyio.fail_after(5):
        result = await serve_change_session(
            change, run_fn=fake_run, hibernate=False, non_interactive=True)
    assert result == "done"
    assert captured["headless"] is True


def test_spawn_session_daemon_emits_non_interactive_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    import imp.remote.session.attach as attach_mod

    change = _make_change(tmp_path, "2026-06-15-spwn")
    captured: dict[str, Any] = {}

    class _FakePopen:
        def __init__(self, argv: list[str], **_kw: Any) -> None:
            captured["argv"] = argv

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    attach_mod.spawn_session_daemon(change, non_interactive=True)
    assert "--non-interactive" in captured["argv"]


def test_spawn_session_daemon_omits_flag_when_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    import imp.remote.session.attach as attach_mod

    change = _make_change(tmp_path, "2026-06-15-spwi")
    captured: dict[str, Any] = {}

    class _FakePopen:
        def __init__(self, argv: list[str], **_kw: Any) -> None:
            captured["argv"] = argv

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    attach_mod.spawn_session_daemon(change)
    assert "--non-interactive" not in captured["argv"]


def test_main_parses_non_interactive_into_serve_kwargs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.remote.session.daemon as daemon

    loaded = object()
    calls: dict[str, Any] = {}
    monkeypatch.setattr(daemon.Change, "load",
                        staticmethod(lambda cr, cid: loaded))

    def fake_run(fn: Any) -> None:
        calls["fn"] = fn

    monkeypatch.setattr(daemon.anyio, "run", fake_run)
    rc = daemon.main([
        "--control-root", str(tmp_path),
        "--change-id", "2026-06-15-mnint", "--non-interactive"])
    assert rc == 0
    fn = calls["fn"]
    assert fn.func is daemon.serve_change_session
    assert fn.keywords == {"non_interactive": True}


def test_build_session_attach_forwards_non_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.remote.session.attach as attach_mod

    change = _make_change(tmp_path, "2026-06-15-bsa")
    captured: dict[str, Any] = {}

    def fake_spawn(change: Change, **kw: Any) -> None:
        captured.update(kw)

    monkeypatch.setattr(attach_mod, "spawn_session_daemon", fake_spawn)

    holder: dict[str, Any] = {}

    async def fake_attach_or_spawn(
        change: Change, *, on_event: Any, spawn_fn: Any,
        connect_timeout: float = 10.0, lease: bool = False,
    ) -> object:
        holder["spawn_fn"] = spawn_fn
        return object()

    monkeypatch.setattr(attach_mod, "attach_or_spawn", fake_attach_or_spawn)

    attach = attach_mod.build_session_attach(change, non_interactive=True)
    anyio.run(attach, lambda _e: None)
    holder["spawn_fn"](change)
    assert captured["non_interactive"] is True


def test_build_session_attach_forwards_autonomous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.remote.session.attach as attach_mod

    change = _make_change(tmp_path, "2026-06-15-bsa-auto")
    captured: dict[str, Any] = {}

    def fake_spawn(change: Change, **kw: Any) -> None:
        captured.update(kw)

    monkeypatch.setattr(attach_mod, "spawn_session_daemon", fake_spawn)

    holder: dict[str, Any] = {}

    async def fake_attach_or_spawn(
        change: Change, *, on_event: Any, spawn_fn: Any,
        connect_timeout: float = 10.0, lease: bool = False,
    ) -> object:
        holder["spawn_fn"] = spawn_fn
        return object()

    monkeypatch.setattr(attach_mod, "attach_or_spawn", fake_attach_or_spawn)

    attach = attach_mod.build_session_attach(change, autonomous=True)
    anyio.run(attach, lambda _e: None)
    holder["spawn_fn"](change)
    assert captured["autonomous"] is True


@pytest.mark.anyio
async def test_resume_loop_under_non_interactive_passes_flag_to_second_run(
    tmp_path: Path,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-nirl")
    sock = str(session_socket_path(change.control_root, change.id))
    received_flags: list[bool] = []

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
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
            result["r"] = await serve_change_session(
                change, run_fn=fake_run, hibernate=False, non_interactive=True)

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        received: list[dict[str, Any]] = []
        client = SessionClient(sock, change.id, change.title,
                               on_event=received.append)
        await client.connect()
        tg.start_soon(client.receive_events)
        with anyio.fail_after(5):
            while not any(e.get("type") == "awaiting_user_input" for e in received):
                await anyio.sleep(0.02)
        await client.send_input("redirect")
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] is RunResult.COMPLETED
        assert received_flags == [True, True]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_resume_loop_under_autonomous_passes_flag_to_second_run(
    tmp_path: Path,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-arl")
    sock = str(session_socket_path(change.control_root, change.id))
    received_flags: list[bool] = []

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
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
            result["r"] = await serve_change_session(
                change, run_fn=fake_run, hibernate=False, autonomous=True)

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        received: list[dict[str, Any]] = []
        client = SessionClient(sock, change.id, change.title,
                               on_event=received.append)
        await client.connect()
        tg.start_soon(client.receive_events)
        with anyio.fail_after(5):
            while not any(e.get("type") == "awaiting_user_input" for e in received):
                await anyio.sleep(0.02)
        await client.send_input("redirect")
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] is RunResult.COMPLETED
        assert received_flags == [True, True]
        await client.aclose()
        tg.cancel_scope.cancel()


def test_main_forwards_autonomous_via_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import functools

    import imp.remote.session.daemon as daemon

    loaded = object()
    captured: dict[str, Any] = {}

    def fake_load(control_root: Path, change_id: str) -> object:
        return loaded

    def fake_run(fn: Any) -> None:
        captured["fn"] = fn

    monkeypatch.setattr(daemon.Change, "load", staticmethod(fake_load))
    monkeypatch.setattr(daemon.anyio, "run", fake_run)

    rc = daemon.main([
        "--control-root", str(tmp_path),
        "--change-id", "2026-06-15-abc",
        "--autonomous",
    ])

    assert rc == 0
    fn = captured["fn"]
    assert isinstance(fn, functools.partial)
    assert fn.func is daemon.serve_change_session
    assert fn.keywords == {"autonomous": True}


@pytest.mark.anyio
async def test_autonomous_forwarded_to_run_fn(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-aut")
    captured: dict[str, Any] = {}

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None, autonomous: bool = False,
    ) -> RunResult:
        captured["autonomous"] = autonomous
        return RunResult.COMPLETED

    with anyio.fail_after(5):
        result = await serve_change_session(
            change, run_fn=fake_run, hibernate=False, autonomous=True)
    assert result is RunResult.COMPLETED
    assert captured["autonomous"] is True


@pytest.mark.anyio
async def test_detach_at_hold_prompt_stamps_resume_target_prompt(
    tmp_path: Path,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-dph")
    st = State.load(change.state_dir)
    st.step = "code"               # the active step at detach time
    st.resume_pending = "review"   # already armed by the executor Pause handler
    st.interrupt_pending = True
    st.save(change.state_dir)

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        await coordinator.signal_pause(reason="ctrl-c")
        return RunResult.PAUSED

    with anyio.fail_after(5):
        result = await serve_change_session(
            change, run_fn=fake_run, hibernate=True, first_attach_timeout=0.1)

    assert result is RunResult.PAUSED
    state = State.load(change.state_dir)
    assert state.resume_target == "prompt"
    assert state.resume_pending == "review"   # untouched by the non-ctrl-d branch
    assert state.interrupt_pending is True     # untouched by the non-ctrl-d branch


@pytest.mark.anyio
async def test_detach_after_ctrl_d_midrun_stamps_resume_target_agent(
    tmp_path: Path,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-cda")
    st = State.load(change.state_dir)
    st.step = "review"
    st.save(change.state_dir)

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        await coordinator.signal_pause(reason="ctrl-d")
        return RunResult.PAUSED

    with anyio.fail_after(5):
        result = await serve_change_session(
            change, run_fn=fake_run, hibernate=True, first_attach_timeout=0.1)

    assert result is RunResult.PAUSED
    state = State.load(change.state_dir)
    assert state.resume_target == "agent"
    assert state.resume_pending == "review"   # arm_step_resume(step) → native resume
    assert state.interrupt_pending is True     # continuation nudge on no-message resume


@pytest.mark.anyio
async def test_resume_prompt_target_holds_before_running_agent(
    tmp_path: Path,
) -> None:
    import imp.remote.session.daemon as daemon

    change = _make_change(tmp_path, "2026-06-15-rpt")
    st = State.load(change.state_dir)
    st.resume_target = "prompt"
    st.save(change.state_dir)
    sock = str(session_socket_path(change.control_root, change.id))

    calls: dict[str, Any] = {"n": 0, "interject": None}

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        calls["n"] += 1
        calls["interject"] = coordinator.drain_interject()
        on_event({"type": "agent_output", "payload": {}})
        return RunResult.COMPLETED

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_change_session(
                change, run_fn=fake_run, hibernate=False, resume=True)

        tg.start_soon(_serve)
        await _wait_for_socket(sock)
        received: list[dict[str, Any]] = []
        client = SessionClient(sock, change.id, change.title,
                               on_event=received.append)
        await client.connect()
        tg.start_soon(client.receive_events)
        with anyio.fail_after(5):
            while not any(
                    e.get("type") == "awaiting_user_input" for e in received):
                await anyio.sleep(0.02)
        # The hold prompt is surfaced FIRST — the agent has not run yet.
        assert calls["n"] == 0
        prompts = [e["payload"]["prompt"] for e in received
                   if e.get("type") == "awaiting_user_input"]
        assert prompts[-1] == daemon._RESUME_PROMPT
        await client.send_input("steer me")
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
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
    change = _make_change(tmp_path, "2026-06-15-rat")
    st = State.load(change.state_dir)
    st.resume_target = target
    st.save(change.state_dir)

    seen: dict[str, bool] = {"ran": False}

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
    ) -> RunResult:
        seen["ran"] = True
        return RunResult.COMPLETED

    with anyio.fail_after(5):
        result = await serve_change_session(
            change, run_fn=fake_run, hibernate=False, resume=True)

    assert result is RunResult.COMPLETED
    assert seen["ran"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("flag", ["non_interactive", "autonomous"])
async def test_resume_prompt_target_runs_agent_without_interactive_channel(
    tmp_path: Path, flag: str,
) -> None:
    """A resume_target=='prompt' resteer hold only makes sense when a human can
    type. A headless or autonomous resume has no channel to answer the prompt, so
    honoring it would re-pause without progress — it must continue the agent
    instead (§6.15 mid-step ctrl-c resume completes the interrupted step)."""
    change = _make_change(tmp_path, "2026-06-15-rph")
    st = State.load(change.state_dir)
    st.resume_target = "prompt"
    st.save(change.state_dir)

    seen: dict[str, bool] = {"ran": False}

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, wait_for_turn_fn: Any = None,
        headless: bool = False, autonomous: bool = False,
    ) -> RunResult:
        seen["ran"] = True
        return RunResult.COMPLETED

    kwargs = {flag: True}
    with anyio.fail_after(5):
        result = await serve_change_session(
            change, run_fn=fake_run, hibernate=True, resume=True,
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

        async def signal_pause(self, *, reason: str = "ctrl-c") -> None:
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

        async def signal_pause(self, *, reason: str = "ctrl-c") -> None:
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
