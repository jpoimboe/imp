from __future__ import annotations

import json
import subprocess
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
from imp.engine import archive
from imp.engine.change import Change
from imp.engine.executor import RunResult
from imp.engine.state import State
from imp.remote.session.discover import session_socket_path
from imp.remote.session.server import SessionServer
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_change(tmp_path: Path, quest_id: str) -> Change:
    return Change(
        id=quest_id,
        title="T",
        base_branch="main",
        control_root=tmp_path,
        project_root=tmp_path,
        quest_dir=tmp_path / ".imp" / "change",
    )


def test_infer_session_result_completed_when_merged_no_worktree(tmp_path: Path) -> None:
    # A merged change prunes the worktree but its state survives at the root home.
    change = _make_change(tmp_path, "2026-06-15-merged")
    State(id=change.id, step=None, step_attempt=0, status="completed").save(
        change.state_dir)
    assert infer_session_result(change) is RunResult.COMPLETED


def test_infer_session_result_paused_for_parked_draft_no_worktree(tmp_path: Path) -> None:
    # A parked draft: never materialized (no worktree), spec phase paused.
    change = _make_change(tmp_path, "2026-06-15-draft")
    State(id=change.id, step="spec-writer", step_attempt=0, status="paused").save(
        change.state_dir)
    assert infer_session_result(change) is RunResult.PAUSED


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
        lambda ch, **_kw: None)

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
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...

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
        lambda ch, **_kw: None)

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
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...

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
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...

    async def _solicit(_prompt: str) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    # The durable `interrupted` flag (set by signal_pause on a real Ctrl-C) is what
    # _session_exit_code reads for 130 — set it ALONE (should_pause stays False) so
    # this test fails if the exit-code guard regresses to reading should_pause.
    coord._interrupted = True
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
    it as the gate reply. The gate's prompt text (the interrupt-steer hint or the
    daemon's resume prompt) is threaded through to ask_line_with_slash as the
    field `hint`; it also threads the run's change, status, driver, and
    coordinator."""
    change = _make_change(tmp_path, "2026-06-15-interactive")

    class _DummyPrompt:
        def __init__(self, **_kw: object) -> None: ...

    monkeypatch.setattr("imp.cli.session_run.Prompt", _DummyPrompt)

    captured: dict[str, object] = {}

    async def _fake_ask(
        prompt: object, status: object, ch: object, *,
        color: bool, width: int, coord: object, driver: object,
        hint: str | None = None,
    ) -> str:
        captured["change"] = ch
        captured["status"] = status
        captured["driver"] = driver
        captured["coord"] = coord
        captured["hint"] = hint
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
    assert captured["hint"] == "daemon wait_for_user prompt text"


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
        ch: Change, *, resume: bool = False,
        non_interactive: bool = False, autonomous: bool = False,
        lease: bool = False, model: str | None = None,
    ) -> Any:
        captured["resume"] = resume
        captured["lease"] = lease

        async def _attach(_on_event: Any) -> Any:  # pragma: no cover - never run
            raise AssertionError("attach must not be invoked in this test")

        return _attach

    monkeypatch.setattr("imp.cli.session_run.build_session_attach", _fake_build)

    async def _noop(**_kwargs: object) -> None:
        pass

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...

    async def _solicit(_prompt: str) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    rc = await run_session_client(
        change, driver=_Driver(), coord=coord, solicit=_solicit, resume=True)

    assert captured["resume"] is True
    assert captured["lease"] is True   # the terminal path leases
    assert rc == 0



class _NullDriver:
    def on_event(self, _e: dict[str, Any]) -> None: ...
    def finish(self) -> None: ...
    def seed_pending_reply(self, _text: str) -> None: ...
    def install_startup_status(self) -> None: ...


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
    # State is completed but no worktree: daemon finished before the client connected.
    State(id=change.id, step=None, step_attempt=0, status="completed").save(
        change.state_dir)

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


@pytest.mark.anyio
async def test_run_session_client_forwards_new_leave_mode_to_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    change = Change.create_draft(
        tmp_path, title="Draft", quest_id="d1", base_branch="main")
    change.event_log().append(
        agent_id=None, type="user_message",
        payload={"role": "user", "content": "hi"})

    captured: dict[str, object] = {}

    async def _capture(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _capture)
    monkeypatch.setattr("imp.cli.main._session_exit_code", lambda *_: 0)

    coord = InterruptCoordinator()
    rc = await run_session_client(
        change, driver=_NullDriver(), coord=coord,
        solicit=lambda _p: _never(), leave_mode="new")

    assert captured["leave_mode"] == "new"
    # The foreground does NOT apply the disposition — the daemon owns it.
    assert (tmp_path / ".imp" / "changes" / "d1").exists()
    assert rc == 0


@pytest.mark.anyio
async def test_run_session_client_forwards_chat_leave_mode_to_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    change = Change.create_draft(
        tmp_path, title="Draft", quest_id="c1", base_branch="main")
    change.event_log().append(
        agent_id=None, type="opening_intent", payload={"text": "a question"})

    captured: dict[str, object] = {}

    async def _capture(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _capture)
    monkeypatch.setattr("imp.cli.main._session_exit_code", lambda *_: 0)

    coord = InterruptCoordinator()
    rc = await run_session_client(
        change, driver=_NullDriver(), coord=coord,
        solicit=lambda _p: _never(), leave_mode="chat")

    assert captured["leave_mode"] == "chat"
    assert (tmp_path / ".imp" / "changes" / "c1").exists()
    assert rc == 0


@pytest.mark.anyio
async def test_run_session_client_passes_terminal_title_from_quest_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.cli.terminal_title import TerminalTitle

    change = _make_change(tmp_path, "2026-06-15-title")   # _make_change sets title="T"
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step=None, step_attempt=0, status="completed").save(
        change.state_dir)

    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _capture)
    monkeypatch.setattr("imp.cli.main._handle_terminal_outcome", lambda *_: 0)

    coord = InterruptCoordinator()
    rc = await run_session_client(
        change, driver=_NullDriver(), coord=coord, solicit=lambda _p: _never())

    title = captured["title"]
    assert isinstance(title, TerminalTitle)
    assert title._name == "T"
    assert title._quest_id == "2026-06-15-title"
    assert rc == 0


@pytest.mark.anyio
async def test_run_session_client_passes_control_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-cr")
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step=None, step_attempt=0, status="completed").save(
        change.state_dir)

    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _capture)
    monkeypatch.setattr("imp.cli.main._handle_terminal_outcome", lambda *_: 0)

    coord = InterruptCoordinator()
    rc = await run_session_client(
        change, driver=_NullDriver(), coord=coord, solicit=lambda _p: _never())

    assert captured["control_root"] == change.control_root
    assert rc == 0


@pytest.mark.anyio
async def test_run_session_client_resets_title_on_daemon_startup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Worktree exists + non-completed state → infer_session_result returns PAUSED,
    # so this is the rc=3 daemon-startup-failure path (not a fast-completed run).
    change = _make_change(tmp_path, "2026-06-15-leak")
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step="plan", step_attempt=0, status="paused").save(
        change.state_dir)

    reset_calls: list[str] = []

    class _FakeTitle:
        def __init__(self, stream: object, quest_id: str, change_name: str) -> None:
            self._quest_id = quest_id
            self._name = change_name

        def working(self) -> None: ...
        def waiting(self) -> None: ...

        def reset(self) -> None:
            reset_calls.append("reset")

    # TimeoutError is raised by attach BEFORE run_terminal_client enters the try
    # whose finally resets the title, so run_session_client must reset it itself.
    monkeypatch.setattr("imp.cli.session_run.TerminalTitle", _FakeTitle)

    async def _boom(**_kwargs: object) -> None:
        raise TimeoutError

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _boom)
    monkeypatch.setattr("imp.cli.main._handle_terminal_outcome", lambda *_: 1)

    coord = InterruptCoordinator()
    rc = await run_session_client(
        change, driver=_NullDriver(), coord=coord, solicit=lambda _p: _never())

    assert rc == 3
    assert reset_calls == ["reset"]


@pytest.mark.anyio
async def test_run_session_client_threads_provided_leave_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-ls")
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step=None, step_attempt=0, status="completed").save(
        change.state_dir)

    async def _capture(**kwargs: object) -> None:
        kwargs["leave_state"]["intent"] = "clear"  # gate-loop populates the dict

    seen: dict[str, object] = {}

    def _fake_disposition(change: object, leave_state: object) -> None:
        seen["leave_state"] = leave_state
        return None

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _capture)
    monkeypatch.setattr(
        "imp.cli.main._print_leave_disposition", _fake_disposition)
    monkeypatch.setattr("imp.cli.main._session_exit_code", lambda *_: 0)

    coord = InterruptCoordinator()
    ls: dict[str, str] = {}
    rc = await run_session_client(
        change, driver=_NullDriver(), coord=coord, solicit=lambda _p: _never(),
        leave_state=ls)

    assert ls["intent"] == "clear"        # the caller's dict surfaced the signal
    assert seen["leave_state"] is ls      # same dict fed to the disposition
    assert rc == 0


@pytest.mark.anyio
async def test_run_session_client_reuses_passed_in_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A completed change with no worktree → infer_session_result COMPLETED, rc 0.
    change = _make_change(tmp_path, "2026-06-15-reuse")
    State(id=change.id, step=None, step_attempt=0, status="completed").save(
        change.state_dir)

    # If run_session_client builds its own title despite one being passed, this fires.
    def _no_construct(*_a: object, **_k: object) -> object:
        raise AssertionError(
            "run_session_client must not construct a title when one is passed")

    monkeypatch.setattr("imp.cli.session_run.TerminalTitle", _no_construct)

    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _capture)
    monkeypatch.setattr("imp.cli.main._handle_terminal_outcome", lambda *_: 0)

    class _SentinelTitle:
        def __init__(self) -> None:
            self._quest_id = "sentinel"
            self._name = "sentinel"

        def working(self) -> None: ...
        def waiting(self) -> None: ...
        def reset(self) -> None: ...

    sentinel = _SentinelTitle()

    coord = InterruptCoordinator()
    rc = await run_session_client(
        change, driver=_NullDriver(), coord=coord,
        solicit=lambda _p: _never(), title=sentinel)

    assert captured["title"] is sentinel   # the exact instance flowed through
    assert rc == 0


@pytest.mark.anyio
async def test_run_session_client_threads_autonomous_into_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-aut")
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step=None, step_attempt=0, status="completed").save(
        change.state_dir)

    captured: dict[str, object] = {}

    def _fake_build(
        ch: Change, *, resume: bool = False,
        non_interactive: bool = False, autonomous: bool = False,
        lease: bool = False, model: str | None = None,
    ) -> Any:
        captured["autonomous"] = autonomous

        async def _attach(_on_event: Any) -> Any:  # pragma: no cover - never run
            raise AssertionError("attach must not be invoked in this test")

        return _attach

    monkeypatch.setattr("imp.cli.session_run.build_session_attach", _fake_build)

    async def _noop(**_kwargs: object) -> None:
        pass

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...

    async def _solicit(_prompt: str) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    rc = await run_session_client(
        change, driver=_Driver(), coord=coord, solicit=_solicit, autonomous=True)
    assert rc == 0
    assert captured["autonomous"] is True


def test_should_run_ctrl_d_reader_requires_interactive_tty(monkeypatch) -> None:
    from imp.cli.session_run import _should_run_ctrl_d_reader

    class _FakeStdin:
        def __init__(self, tty: bool) -> None:
            self._tty = tty

        def isatty(self) -> bool:
            return self._tty

    monkeypatch.setattr("sys.stdin", _FakeStdin(True))
    assert _should_run_ctrl_d_reader(non_interactive=False) is True
    assert _should_run_ctrl_d_reader(non_interactive=True) is False

    monkeypatch.setattr("sys.stdin", _FakeStdin(False))
    assert _should_run_ctrl_d_reader(non_interactive=False) is False


def test_infer_session_result_completed_from_archive_when_home_reaped(tmp_path: Path) -> None:
    quest_id = "2026-07-03-reaped"
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)

    events_file = tmp_path / f"{quest_id}.jsonl"
    events_file.write_bytes(
        f'{{"id":"{quest_id}-e0","parent_id":null,"agent_id":null,'
        '"timestamp":"2026-06-01T00:00:01+00:00","type":"change_created",'
        f'"payload":{{"id":"{quest_id}"}}}}\n'.encode()
    )
    change_json = tmp_path / f"{quest_id}-change.json"
    change_json.write_text(json.dumps({
        "id": quest_id, "title": "Reaped", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / f"{quest_id}-state.json"
    state_json.write_text(json.dumps({"id": quest_id, "status": "completed", "step": None}))
    archive.archive_change(
        tmp_path, quest_id, events_path=events_file,
        change_json_path=change_json, state_json_path=state_json)

    # Build the Change WITHOUT creating the live home/state dir — State.load raises
    # FileNotFoundError, forcing the archive fallback branch.
    change = _make_change(tmp_path, quest_id)
    assert not change.state_dir.exists()
    assert infer_session_result(change) is RunResult.COMPLETED


@pytest.mark.anyio
async def test_run_session_client_wires_seed_pending_reply_as_local_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    change = Change.create_draft(
        tmp_path, title="Draft", quest_id="e1", base_branch="main")
    change.event_log().append(
        agent_id=None, type="user_message",
        payload={"role": "user", "content": "hi"})

    captured: dict[str, object] = {}

    async def _capture(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _capture)
    monkeypatch.setattr("imp.cli.main._session_exit_code", lambda *_: 0)

    driver = _NullDriver()
    coord = InterruptCoordinator()
    rc = await run_session_client(
        change, driver=driver, coord=coord, solicit=lambda _p: _never())

    assert captured["local_echo"] == driver.seed_pending_reply
    assert rc == 0


@pytest.mark.anyio
async def test_run_session_client_installs_startup_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-startup")
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step=None, step_attempt=0, status="completed").save(
        change.state_dir)

    async def _noop(**_kwargs: object) -> None:
        pass

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)
    monkeypatch.setattr("imp.cli.main._handle_terminal_outcome", lambda *_: 0)

    installed: list[bool] = []

    class _Driver(_NullDriver):
        def install_startup_status(self) -> None:
            installed.append(True)

    coord = InterruptCoordinator()
    rc = await run_session_client(
        change, driver=_Driver(), coord=coord, solicit=lambda _p: _never())

    assert installed == [True]           # called exactly once
    assert rc == 0


@pytest.mark.anyio
async def test_run_session_client_threads_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(tmp_path, "2026-06-15-model")
    (tmp_path / ".imp" / "worktrees" / change.id).mkdir(parents=True)
    State(id=change.id, step=None, step_attempt=0, status="completed").save(
        change.state_dir)

    captured: dict[str, object] = {}

    def _fake_build(ch: Any, **kw: Any) -> Any:
        captured.update(kw)

        async def _attach(_on_event: Any) -> Any:  # pragma: no cover
            raise AssertionError("attach must not be invoked in this test")

        return _attach

    monkeypatch.setattr("imp.cli.session_run.build_session_attach", _fake_build)

    async def _noop(**_kwargs: object) -> None:
        pass

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)

    class _Driver:
        def on_event(self, _event: Any) -> None: ...
        def finish(self) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...

    async def _solicit(_prompt: str) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    rc = await run_session_client(
        change, driver=_Driver(), coord=coord, solicit=_solicit,
        model="anthropic:claude-fable-5")

    assert rc == 0
    assert captured["model"] == "anthropic:claude-fable-5"
