from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from imp.engine.change import Change
from imp.engine.executor import RunResult
from imp.remote.session.client import SessionClient
from imp.remote.session.daemon import _is_interrupt_pause, serve_change_session
from imp.remote.session.discover import session_socket_path
from imp.remote.session.protocol import CONTROL_ABANDON


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_change(tmp_path: Path, change_id: str) -> Change:
    return Change(
        id=change_id,
        title="T",
        base_branch="main",
        control_root=tmp_path,
        project_root=tmp_path,
        change_dir=tmp_path / ".imp" / "change",
    )


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
    from imp.agents.tools.wait_for_user import AwaitingUserInput

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
        result = await serve_change_session(change, run_fn=fake_run)
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
        await anyio.sleep(0.05)  # let the resume gate open (next_input is awaiting)
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
            change, run_fn=fake_run, hibernate=True)
    assert result is RunResult.PAUSED
    assert calls["n"] == 1


def test_main_forwards_seed_opening_via_partial(
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
        "--seed-opening", "ship the thing",
    ])

    assert rc == 0
    fn = captured["fn"]
    assert isinstance(fn, functools.partial)
    assert fn.func is daemon.serve_change_session
    assert fn.args == (loaded,)
    assert fn.keywords == {"seed_opening": "ship the thing"}


@pytest.mark.anyio
async def test_seed_opening_is_forwarded_to_the_first_run_only(
    tmp_path: Path,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-sed")
    sock = str(session_socket_path(change.control_root, change.id))
    seeds: list[str | None] = []

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, seed_opening: str | None = None,
        wait_for_turn_fn: Any = None,
    ) -> RunResult:
        seeds.append(seed_opening)
        if len(seeds) == 1:
            await coordinator.signal_pause(reason="ctrl-c")
            return RunResult.PAUSED
        return RunResult.COMPLETED

    async with anyio.create_task_group() as tg:
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await serve_change_session(
                change, run_fn=fake_run, hibernate=False,
                seed_opening="open the door")

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
        await anyio.sleep(0.05)  # let the resume gate open (next_input is awaiting)
        await client.send_input("go")
        with anyio.fail_after(5):
            while "r" not in result:
                await anyio.sleep(0.02)
        assert result["r"] is RunResult.COMPLETED
        assert seeds == ["open the door", None]
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_resume_marks_running_and_journals_workflow_resumed(
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
        # The prep must already be visible to the run: status flipped to running.
        seen["status"] = State.load(change.state_dir).status
        return RunResult.COMPLETED

    with anyio.fail_after(5):
        result = await serve_change_session(
            change, run_fn=fake_run, hibernate=False, resume=True)

    assert result is RunResult.COMPLETED
    assert seen["status"] == "running"          # status flipped before the run
    assert change.events_path.exists() and "workflow_resumed" in change.events_path.read_text()


@pytest.mark.anyio
async def test_resume_sets_resume_pending_for_a_crashed_run(tmp_path: Path) -> None:
    """A hard-killed run leaves status='running' on disk and never set
    resume_pending. P1: daemon resume prep must set resume_pending to the current
    step so the executor replays its transcript instead of re-running it fresh."""
    from imp.engine.state import State
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    change = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    st = State.load(change.state_dir)
    st.step = "review"
    st.status = "running"          # crashed: status on disk, lock freed by the OS
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
    assert seen["resume_pending"] == "review"


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


def test_main_forwards_both_seed_and_resume_via_partial(
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
        "--seed-opening", "hello seed",
        "--resume",
    ])

    assert rc == 0
    fn = captured["fn"]
    assert isinstance(fn, functools.partial)
    assert fn.func is daemon.serve_change_session
    assert fn.args == (loaded,)
    assert fn.keywords == {"seed_opening": "hello seed", "resume": True}


def test_main_forwards_from_target_via_partial(
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
        "--resume",
        "--from=review",
    ])

    assert rc == 0
    fn = captured["fn"]
    assert isinstance(fn, functools.partial)
    assert fn.func is daemon.serve_change_session
    assert fn.args == (loaded,)
    assert fn.keywords == {"resume": True, "from_target": "review"}


def test_main_forwards_from_target_without_resume(
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
        "--from=review",
    ])

    assert rc == 0
    fn = captured["fn"]
    assert isinstance(fn, functools.partial)
    assert fn.keywords == {"from_target": "review"}


def _seed_control_default(root: Path) -> None:
    import shutil

    from imp.assets import bundled_workflow_dir
    from tests.conftest import _git, _init_repo

    _init_repo(root)
    shutil.copytree(bundled_workflow_dir("imp"), root / "imp" / "workflows" / "imp")
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(
        "providers: [anthropic]\n"
        "tiers:\n  low: {anthropic: m}\n  medium: {anthropic: m}\n  high: {anthropic: m}\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup (default workflow)")


@pytest.mark.anyio
async def test_resume_from_applies_rewind_under_the_daemon_lock(
    tmp_path: Path,
) -> None:
    from imp.engine.state import State

    _seed_control_default(tmp_path)
    change = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    st = State.load(change.state_dir)
    st.step = "review"
    st.status = "paused"
    st.loop_counts = {"task-lead": 2, "review": 6}
    st.save(change.state_dir)

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> RunResult:
        return RunResult.COMPLETED

    with anyio.fail_after(5):
        result = await serve_change_session(
            change, run_fn=fake_run, hibernate=False,
            resume=True, from_target="review")

    assert result is RunResult.COMPLETED

    types = [e["type"] for e in change.event_log().read()]
    assert "workflow_resumed" in types and "step_rewound" in types
    assert types.index("workflow_resumed") < types.index("step_rewound")
    assert "gates_bypassed" not in types

    final = State.load(change.state_dir)

    assert final.loop_counts == {"task-lead": 2}   # review reset, upstream kept


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


@pytest.mark.anyio
async def test_invalid_from_target_is_rejected_before_any_log_mutation(
    tmp_path: Path,
) -> None:
    from imp.engine import recovery
    from imp.engine.state import State

    _seed_control_default(tmp_path)
    change = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    st = State.load(change.state_dir)
    st.step = "review"
    st.status = "paused"
    st.save(change.state_dir)

    async def fake_run(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any,
        wait_for_turn_fn: Any = None,
    ) -> RunResult:
        raise AssertionError("run must not start when from_target is invalid")

    with pytest.raises(recovery.RewindError):
        await serve_change_session(
            change, run_fn=fake_run, hibernate=False, resume=True,
            from_target="definitely-not-a-step")

    types = [e["type"] for e in change.event_log().read()]
    assert "workflow_resumed" not in types


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


def test_append_workflow_resumed_idempotent_across_rewind_window(
    tmp_path: Path,
) -> None:
    import imp.remote.session.daemon as daemon
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    change = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    log = change.event_log()
    daemon._append_workflow_resumed(log)
    # apply_rewind appends exactly one step_rewound AFTER workflow_resumed.
    log.append(agent_id=None, type="step_rewound", payload={})
    daemon._append_workflow_resumed(log)   # trailing step_rewound → still idempotent
    assert [e["type"] for e in log.read()].count("workflow_resumed") == 1


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
