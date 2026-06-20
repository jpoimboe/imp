from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.cli.session_run import (
    infer_session_result,
    make_headless_solicit,
    make_interactive_solicit,
    run_session_client,
)
from imp.cli.signals import InterruptCoordinator
from imp.engine.change import Change
from imp.engine.executor import RunResult
from imp.engine.state import State
from imp.remote.session.discover import session_socket_path
from imp.remote.session.server import SessionServer


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


def test_infer_session_result_completed_when_worktree_removed(tmp_path: Path) -> None:
    # A merge-completing run removes the worktree (and the state dir inside it).
    change = _make_change(tmp_path, "2026-06-15-merged")
    assert infer_session_result(change) is RunResult.COMPLETED


def test_infer_session_result_completed_when_status_completed(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-done")
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step=None, step_attempt=0, status="completed").save(
        change.state_dir)
    assert infer_session_result(change) is RunResult.COMPLETED


def test_infer_session_result_paused_when_parked_at_gate(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-parked")
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step="plan", step_attempt=0, status="paused").save(
        change.state_dir)
    assert infer_session_result(change) is RunResult.PAUSED


def test_infer_session_result_paused_when_merge_blocked(tmp_path: Path) -> None:
    change = _make_change(tmp_path, "2026-06-15-blocked")
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step=None, step_attempt=0, status="merge_blocked").save(
        change.state_dir)
    assert infer_session_result(change) is RunResult.PAUSED


@pytest.mark.anyio
async def test_run_session_client_answers_gate_and_returns_completed_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-run")
    # The on-disk terminal state the client reads once the daemon exits.
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step=None, step_attempt=0, status="completed").save(
        change.state_dir)

    # No real subprocess: in-process server stands in; spawn is a no-op.
    monkeypatch.setattr(
        "imp.remote.session.attach.spawn_session_daemon",
        lambda ch, *, seed_opening=None: None)

    sock = str(session_socket_path(change.control_root, change.id))
    server = SessionServer(sock, hibernate=False)
    answered: list[str] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(10):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "name?"}})
        answered.append(await user_chat_input_fn("name?"))
        return "completed"

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _solicit(_prompt: str) -> str:
        return "imp"

    coord = InterruptCoordinator()
    rc_holder: dict[str, int] = {}
    with anyio.fail_after(30):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _go() -> None:
                rc_holder["rc"] = await run_session_client(
                    change, driver=_Driver(), coord=coord, solicit=_solicit)

            tg.start_soon(_go)
            with anyio.fail_after(15):
                while "rc" not in rc_holder:
                    await anyio.sleep(0.02)
            tg.cancel_scope.cancel()

    assert answered == ["imp"]
    assert rc_holder["rc"] == 0


@pytest.mark.anyio
async def test_run_session_client_starts_status_tick_when_provided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-tick")
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step=None, step_attempt=0, status="completed").save(
        change.state_dir)

    monkeypatch.setattr(
        "imp.remote.session.attach.spawn_session_daemon",
        lambda ch, *, seed_opening=None: None)

    sock = str(session_socket_path(change.control_root, change.id))
    server = SessionServer(sock, hibernate=False)
    tick_started = anyio.Event()

    class _Status:
        async def tick(self) -> None:
            tick_started.set()
            await anyio.sleep_forever()

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        with anyio.fail_after(10):
            while server.attached_count == 0:
                await anyio.sleep(0.02)
        return "completed"

    async def _solicit(_prompt: str) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    rc_holder: dict[str, int] = {}
    with anyio.fail_after(30):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _go() -> None:
                rc_holder["rc"] = await run_session_client(
                    change, driver=_Driver(), coord=coord,
                    solicit=_solicit, status=_Status())

            tg.start_soon(_go)
            with anyio.fail_after(15):
                while "rc" not in rc_holder:
                    await anyio.sleep(0.02)
            tg.cancel_scope.cancel()

    assert tick_started.is_set()
    assert rc_holder["rc"] == 0


@pytest.mark.anyio
async def test_run_session_client_returns_130_when_user_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-interrupted")
    # Worktree exists + paused status → infer_session_result returns PAUSED.
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step="plan", step_attempt=0, status="paused").save(
        change.state_dir)

    async def _noop(**_kwargs: object) -> None:
        pass

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)
    monkeypatch.setattr("imp.cli.main._handle_terminal_outcome", lambda *_: 1)

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _solicit(_prompt: str) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    coord._should_pause = True  # simulate Ctrl-C/ESC forwarded to daemon
    rc = await run_session_client(
        change, driver=_Driver(), coord=coord, solicit=_solicit)
    assert rc == 130


@pytest.mark.anyio
async def test_make_headless_solicit_delivers_message_once_then_signals_detach() -> None:
    coord = InterruptCoordinator()
    coord.escalation_event = anyio.Event()  # run_terminal_client sets this before any gate
    solicit = make_headless_solicit("ship it", coord)

    # First gate: the single --message is handed over; no detach yet.
    assert await solicit("first gate?") == "ship it"
    assert not coord.escalation_event.is_set()

    # Second gate: no more input — the solicit must NOT return a (stray) reply; it
    # signals a clean detach and blocks. move_on_after fires only because it blocked.
    with anyio.move_on_after(0.2) as scope:
        await solicit("second gate?")
    assert scope.cancelled_caught
    assert coord.escalation_event.is_set()


@pytest.mark.anyio
async def test_make_headless_solicit_with_no_message_signals_detach_immediately() -> None:
    coord = InterruptCoordinator()
    coord.escalation_event = anyio.Event()
    solicit = make_headless_solicit(None, coord)

    # No message at all (e.g. a resume parked at a gate with nothing to inject):
    # the very first gate signals a detach and blocks.
    with anyio.move_on_after(0.2) as scope:
        await solicit("gate?")
    assert scope.cancelled_caught
    assert coord.escalation_event.is_set()


@pytest.mark.anyio
async def test_make_interactive_solicit_returns_typed_line_and_threads_run_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interactive solicit reads one slash-aware line from the tty and returns
    it as the gate reply, ignoring the daemon's prompt-text argument (the renderer
    already showed that prompt). It threads the run's change, status, driver, and
    coordinator through to ask_line_with_slash."""
    change = _make_change(tmp_path, "2026-06-15-interactive")

    class _DummyPrompt:
        def __init__(self, **_kw: object) -> None: ...

    monkeypatch.setattr("imp.cli.session_run.Prompt", _DummyPrompt)

    captured: dict[str, object] = {}

    async def _fake_ask(
        prompt: object, status: object, ch: object, *,
        color: bool, width: int, coord: object, driver: object,
    ) -> str:
        captured["change"] = ch
        captured["status"] = status
        captured["driver"] = driver
        captured["coord"] = coord
        return "user typed this"

    monkeypatch.setattr("imp.cli.session_run.ask_line_with_slash", _fake_ask)

    coord = InterruptCoordinator()
    status = object()
    driver = object()
    solicit = make_interactive_solicit(
        change, status=status, driver=driver, coord=coord)

    line = await solicit("daemon wait_for_user prompt text")

    assert line == "user typed this"
    assert captured["change"] is change
    assert captured["status"] is status
    assert captured["driver"] is driver
    assert captured["coord"] is coord


@pytest.mark.anyio
async def test_run_session_client_threads_resume_into_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-resume")
    # On-disk terminal state read after the (stubbed) daemon "exits".
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step=None, step_attempt=0, status="completed").save(
        change.state_dir)

    captured: dict[str, object] = {}

    def _fake_build(
        ch: Change, *, seed_opening: str | None = None, resume: bool = False,
        from_target: str | None = None, opening_intent: str | None = None,
    ) -> Any:
        captured["resume"] = resume

        async def _attach(_on_event: Any) -> Any:  # pragma: no cover - never run
            raise AssertionError("attach must not be invoked in this test")

        return _attach

    monkeypatch.setattr("imp.cli.session_run.build_session_attach", _fake_build)

    async def _noop(**_kwargs: object) -> None:
        pass

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)
    monkeypatch.setattr("imp.cli.main._handle_terminal_outcome", lambda *_: 0)

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _solicit(_prompt: str) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    rc = await run_session_client(
        change, driver=_Driver(), coord=coord, solicit=_solicit, resume=True)

    assert captured["resume"] is True
    assert rc == 0


@pytest.mark.anyio
async def test_run_session_client_threads_from_target_into_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-rwd")
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step=None, step_attempt=0, status="completed").save(
        change.state_dir)

    captured: dict[str, object] = {}

    def _fake_build(
        ch: Change, *, seed_opening: str | None = None, resume: bool = False,
        from_target: str | None = None, opening_intent: str | None = None,
    ) -> Any:
        captured["from_target"] = from_target

        async def _attach(_on_event: Any) -> Any:  # pragma: no cover - never run
            raise AssertionError("attach must not be invoked in this test")

        return _attach

    monkeypatch.setattr("imp.cli.session_run.build_session_attach", _fake_build)

    async def _noop(**_kwargs: object) -> None:
        pass

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)
    monkeypatch.setattr("imp.cli.main._handle_terminal_outcome", lambda *_: 0)

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...

    async def _solicit(_prompt: str) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    rc = await run_session_client(
        change, driver=_Driver(), coord=coord, solicit=_solicit,
        resume=True, from_target="review")

    assert captured["from_target"] == "review"
    assert rc == 0


class _NullDriver:
    def on_event(self, _e: dict[str, Any]) -> None: ...
    def finish(self) -> None: ...


async def _never(*_a: Any) -> str:
    raise AssertionError("solicit must not be called")


@pytest.mark.anyio
async def test_daemon_startup_timeout_surfaces_distinctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    change = _make_change(tmp_path, "feat")
    # Worktree exists in non-completed state → daemon crashed, not fast-completed.
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step="plan", step_attempt=0, status="paused").save(
        change.state_dir)

    async def _boom(**_kwargs: Any) -> None:
        raise TimeoutError

    monkeypatch.setattr(session_run, "run_terminal_client", _boom)
    summarized: list[Any] = []
    monkeypatch.setattr(
        "imp.cli.main._handle_terminal_outcome",
        lambda c, r, co: summarized.append(r) or 1)

    coord = InterruptCoordinator()
    rc = await session_run.run_session_client(
        change, driver=_NullDriver(), coord=coord,
        solicit=lambda _p: _never())
    assert rc == 3                       # distinct daemon-startup-failure code
    assert summarized == []              # NOT routed through the paused summary
    assert "daemon" in capsys.readouterr().err.lower()


@pytest.mark.anyio
async def test_daemon_startup_timeout_is_not_rc3_when_change_already_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    change = _make_change(tmp_path, "fast-feat")
    # No worktree → infer_session_result returns COMPLETED:
    # the daemon finished so fast the client timed out connecting, but the run succeeded.

    async def _boom(**_kwargs: Any) -> None:
        raise TimeoutError

    monkeypatch.setattr(session_run, "run_terminal_client", _boom)
    monkeypatch.setattr(
        "imp.cli.main._handle_terminal_outcome",
        lambda c, r, co: 0)

    coord = InterruptCoordinator()
    rc = await session_run.run_session_client(
        change, driver=_NullDriver(), coord=coord,
        solicit=lambda _p: _never())
    assert rc == 0                       # NOT rc=3: change already completed
    assert "daemon" not in capsys.readouterr().err.lower()
