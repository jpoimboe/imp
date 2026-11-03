from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.cli.session_run import (
    make_headless_solicit,
    make_interactive_solicit,
    resolve_settled_outcome,
    run_session_client,
)
from imp.cli.session_terminal import RunWitness
from imp.cli.signals import InterruptCoordinator
from imp.cli.terminal_watchdog import WatchedStream
from imp.engine import archive
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.paths import project_state_dir
from imp.remote.session.client import SessionClient, StreamEnding
from imp.remote.session.discover import session_socket_path
from imp.remote.session.protocol import CONTROL_ABANDON, EndOfRun
from imp.remote.session.server import SessionServer
from tests.async_wait import HANG_BACKSTOP, wait_until
from tests.conftest import _seed_control


async def _no_confirm(question: str) -> bool:
    raise AssertionError(f"unexpected confirm: {question!r}")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_quest(tmp_path: Path, quest_id: str) -> Quest:
    return Quest(
        id=quest_id,
        title="T",
        base_branch="main",
        control_root=tmp_path,
        project_root=tmp_path,
        quest_dir=project_state_dir(tmp_path) / "quests" / quest_id,
    )


def _watched() -> WatchedStream:
    """The leased funnel now requires its wrapper; these tests only need a real one."""
    return WatchedStream(io.StringIO())


def _ending_with(ending: StreamEnding, *events: dict[str, Any]) -> Any:
    """A run_terminal_client stub: the events the client witnessed, folded into the
    witness it returns beside `ending`."""
    async def _stub(**_kwargs: Any) -> tuple[StreamEnding, RunWitness]:
        witness = RunWitness()
        for event in events:
            witness.note(event)
        return ending, witness
    return _stub


def test_resolve_settled_outcome_completed_when_merged_no_worktree(tmp_path: Path) -> None:
    # A merged change prunes the worktree but its state survives at the root home.
    quest = _make_quest(tmp_path, "2026-06-15-merged")
    State(id=quest.id, step=None, step_attempt=0, status="merged").save(
        quest.state_dir)
    outcome = resolve_settled_outcome(quest)
    assert outcome.status == "merged"
    assert outcome.completed is True


def test_resolve_settled_outcome_completed_when_done_no_worktree(tmp_path: Path) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-done")
    State(id=quest.id, step=None, step_attempt=0, status="done").save(quest.state_dir)
    assert resolve_settled_outcome(quest).completed is True


def test_resolve_settled_outcome_not_completed_for_parked_draft(tmp_path: Path) -> None:
    # A parked draft: never materialized (no worktree), spec phase paused.
    quest = _make_quest(tmp_path, "2026-06-15-draft")
    State(id=quest.id, step="spec-writer", step_attempt=0, status="paused").save(
        quest.state_dir)
    assert resolve_settled_outcome(quest).completed is False


def test_resolve_settled_outcome_completed_when_merged_with_worktree(tmp_path: Path) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-mergedwt")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step=None, step_attempt=0, status="merged").save(
        quest.state_dir)
    assert resolve_settled_outcome(quest).completed is True


def test_resolve_settled_outcome_not_completed_when_parked_at_gate(tmp_path: Path) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-parked")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step="plan", step_attempt=0, status="paused").save(
        quest.state_dir)
    assert resolve_settled_outcome(quest).completed is False


def test_resolve_settled_outcome_carries_merge_blocked_status(tmp_path: Path) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-blocked")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step=None, step_attempt=0, status="merge_blocked").save(
        quest.state_dir)
    outcome = resolve_settled_outcome(quest)
    assert outcome.status == "merge_blocked"
    assert outcome.completed is False


def test_resolve_settled_outcome_status_none_when_nothing_persisted(tmp_path: Path) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-gone")
    assert not quest.state_dir.exists()
    outcome = resolve_settled_outcome(quest)
    assert outcome.status is None              # neither home nor archive can answer
    assert outcome.events == []
    assert outcome.completed is False


@pytest.mark.anyio
async def test_run_session_client_answers_gate_and_returns_completed_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    quest = _make_quest(tmp_path, "2026-06-15-run")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)

    # No real subprocess: in-process server stands in; spawn is a no-op.
    monkeypatch.setattr(
        "imp.remote.session.attach.spawn_session_daemon",
        lambda ch, **_kw: None)

    sock = str(session_socket_path(quest.id))
    server = SessionServer(sock, hibernate=False)
    answered: list[str] = []

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "awaiting_user_input", "payload": {"prompt": "name?"}})
        answered.append(await user_chat_input_fn("name?"))
        # Real _complete emits quest_merged before returning (executor.py:995).
        on_event({"type": "quest_merged",
                  "payload": {"base": "main", "commit": "abc123"}})
        return "merged"

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self, **_kw: object) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...
        def show_stop_ack(self, _text: str) -> None: ...
        def clear_stop_ack(self) -> None: ...

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:
        return "imp"

    coord = InterruptCoordinator()
    rc_holder: dict[str, int] = {}
    with anyio.fail_after(HANG_BACKSTOP):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _go() -> None:
                rc_holder["rc"] = await run_session_client(
                    quest, driver=_Driver(), coord=coord, solicit=_solicit, confirm=_no_confirm,
                    leave_mode="new", stream=_watched())

            tg.start_soon(_go)
            await wait_until(lambda: "rc" in rc_holder)
            tg.cancel_scope.cancel()

    out = capsys.readouterr().out
    assert answered == ["imp"]
    assert rc_holder["rc"] == 0
    # The merge was witnessed across the wire; its payload names the base branch.
    assert "merged into main" in out
    assert "done · commits on" not in out


@pytest.mark.anyio
async def test_an_abandon_sent_by_another_client_renders_the_abandoned_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    quest = _make_quest(tmp_path, "2026-09-22-abandon-elsewhere")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    monkeypatch.setattr(
        "imp.remote.session.attach.spawn_session_daemon",
        lambda ch, **_kw: None)

    sock = str(session_socket_path(quest.id))
    server = SessionServer(sock, hibernate=False)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await anyio.sleep_forever()   # only the leave control can end this run
        return "done"

    other = SessionClient(sock, quest.id, "T", on_event=lambda _e: None)
    rc_holder: dict[str, int] = {}
    with anyio.fail_after(HANG_BACKSTOP):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _go() -> None:
                rc_holder["rc"] = await run_session_client(
                    quest, driver=_NullDriver(), coord=InterruptCoordinator(),
                    solicit=lambda _p: _never(),
                    confirm=_no_confirm, leave_mode="new", stream=_watched())

            tg.start_soon(_go)
            await wait_until(lambda: server.leased_count == 1)
            await other.connect()
            await other.send_control(CONTROL_ABANDON)
            await wait_until(lambda: "rc" in rc_holder)
            await other.aclose()
            tg.cancel_scope.cancel()

    out = capsys.readouterr().out
    assert server.abandoned
    assert rc_holder["rc"] == 130
    assert out.endswith(f"{quest.id} · T · abandoned\n\n")
    assert "lost the connection" not in out


@pytest.mark.anyio
async def test_run_session_client_starts_status_tick_when_provided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-tick")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)

    monkeypatch.setattr(
        "imp.remote.session.attach.spawn_session_daemon",
        lambda ch, **_kw: None)

    sock = str(session_socket_path(quest.id))
    server = SessionServer(sock, hibernate=False)
    tick_started = anyio.Event()

    class _Status:
        async def tick(self) -> None:
            tick_started.set()
            await anyio.sleep_forever()

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self, **_kw: object) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...
        def show_stop_ack(self, _text: str) -> None: ...

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
        on_event({"type": "quest_merged",
                  "payload": {"base": "main", "commit": "abc123"}})
        return "merged"

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    rc_holder: dict[str, int] = {}
    with anyio.fail_after(HANG_BACKSTOP):
        async with anyio.create_task_group() as tg:
            await server.open()
            tg.start_soon(server.serve, run_fn)

            async def _go() -> None:
                rc_holder["rc"] = await run_session_client(
                    quest, driver=_Driver(), coord=coord,
                    solicit=_solicit,
                    confirm=_no_confirm, status=_Status(), leave_mode="new", stream=_watched())

            tg.start_soon(_go)
            await wait_until(lambda: "rc" in rc_holder)
            tg.cancel_scope.cancel()

    assert tick_started.is_set()
    assert rc_holder["rc"] == 0


@pytest.mark.anyio
async def test_run_session_client_returns_130_when_user_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-interrupted")
    # What a mid-run SIGINT really produces: the engine emits its interrupt pause
    # and the daemon then concludes, so the client witnesses the pause and sees the
    # end frame. Nothing on disk is consulted.
    monkeypatch.setattr(
        "imp.cli.session_run.run_terminal_client",
        _ending_with(
            StreamEnding.ENDED,
            {"type": "workflow_paused",
             "payload": {"reason": "interrupt", "interrupt_reason": "interrupt"}}))

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self, **_kw: object) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...
        def show_stop_ack(self, _text: str) -> None: ...

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_Driver(), coord=coord, solicit=_solicit, confirm=_no_confirm,
        leave_mode="new", stream=_watched())
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
    # signals a clean detach and blocks. Run it as a task, wait for the detach signal,
    # confirm no reply was produced, then cancel the parked solicit.
    reply: dict[str, str] = {}

    async def _second_gate() -> None:
        reply["r"] = await solicit("second gate?")

    async with anyio.create_task_group() as tg:
        tg.start_soon(_second_gate)
        await wait_until(lambda: coord.escalation_event.is_set())
        assert "r" not in reply
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_make_headless_solicit_with_no_message_signals_detach_immediately() -> None:
    coord = InterruptCoordinator()
    coord.escalation_event = anyio.Event()
    solicit = make_headless_solicit(None, coord)

    # No message at all (e.g. a resume parked at a gate with nothing to inject):
    # the very first gate signals a detach and blocks. Run it as a task, wait for the
    # detach signal, confirm no reply was produced, then cancel the parked solicit.
    reply: dict[str, str] = {}

    async def _first_gate() -> None:
        reply["r"] = await solicit("gate?")

    async with anyio.create_task_group() as tg:
        tg.start_soon(_first_gate)
        await wait_until(lambda: coord.escalation_event.is_set())
        assert "r" not in reply
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_make_headless_solicit_is_spent_by_an_interrupt_gate_without_delivering() -> None:
    coord = InterruptCoordinator()
    coord.escalation_event = anyio.Event()  # run_terminal_client sets this before any gate
    solicit = make_headless_solicit("ship it", coord)

    # An interrupt gate is not the question `--message` answers: the message is
    # discarded, the solicit signals a clean detach and blocks (no stray reply).
    reply: dict[str, str] = {}

    async def _interrupt_gate() -> None:
        reply["first"] = await solicit("steer?", True)

    async with anyio.create_task_group() as tg:
        tg.start_soon(_interrupt_gate)
        await wait_until(lambda: coord.escalation_event.is_set())
        assert "first" not in reply
        tg.cancel_scope.cancel()

    # The latch is spent: a later bare gate does not get the message either.
    coord.escalation_event = anyio.Event()

    async def _later_bare_gate() -> None:
        reply["second"] = await solicit("bare gate")

    async with anyio.create_task_group() as tg:
        tg.start_soon(_later_bare_gate)
        await wait_until(lambda: coord.escalation_event.is_set())
        assert "second" not in reply
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_make_interactive_solicit_returns_typed_line_and_threads_run_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interactive solicit reads one slash-aware line from the tty and returns
    it as the gate reply. The gate's prompt text (the interrupt-steer hint or the
    daemon's resume prompt) is threaded through to ask_line_with_slash as the
    field `hint`; it also threads the run's change, status, driver, and
    coordinator."""
    quest = _make_quest(tmp_path, "2026-06-15-interactive")

    class _DummyPrompt:
        def __init__(self, **_kw: object) -> None: ...
        def seed_history(self, _lines: list[str]) -> None: ...

    monkeypatch.setattr("imp.cli.session_run.Prompt", _DummyPrompt)

    captured: dict[str, object] = {}

    async def _fake_ask(
        prompt: object, status: object, ch: object, *,
        color: bool, width: int, coord: object, driver: object,
        hint: str | None = None, allow_empty: bool = False,
    ) -> str:
        captured["change"] = ch
        captured["status"] = status
        captured["driver"] = driver
        captured["coord"] = coord
        captured["hint"] = hint
        captured["allow_empty"] = allow_empty
        return "user typed this"

    monkeypatch.setattr("imp.cli.session_run.ask_line_with_slash", _fake_ask)

    coord = InterruptCoordinator()
    status = object()
    driver = object()
    solicit = make_interactive_solicit(
        quest, status=status, driver=driver, coord=coord)

    # An interrupt gate (interrupt=True) threads allow_empty=True.
    line = await solicit("daemon wait_for_user prompt text", True)
    assert line == "user typed this"
    assert captured["change"] is quest
    assert captured["status"] is status
    assert captured["driver"] is driver
    assert captured["coord"] is coord
    assert captured["hint"] == "daemon wait_for_user prompt text"
    assert captured["allow_empty"] is True

    # A bare-turn-end gate (interrupt=False) leaves allow_empty=False.
    await solicit("bare turn end", False)
    assert captured["allow_empty"] is False


def test_make_interactive_solicit_seeds_prompt_history_from_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The persistent prompt is seeded at construction from quest.seed_opening
    plus user_message reply texts (engine_message turns excluded), oldest-first."""
    quest = _make_quest(tmp_path, "2026-06-15-seedhist")
    quest.seed_opening = "the seed goal"
    elog = quest.event_log()
    elog.append(
        agent_id=None, type="user_message",
        payload={"role": "user", "subtype": "reply",
                 "content": [{"type": "text", "text": "a human reply"}]})
    elog.append(
        agent_id=None, type="engine_message",
        payload={"subtype": "agent_prompt",
                 "content": [{"type": "text", "text": "engine nudge"}]})

    seeded: dict[str, list[str]] = {}

    class _RecordingPrompt:
        def __init__(self, **_kw: object) -> None: ...
        def seed_history(self, lines: list[str]) -> None:
            seeded["lines"] = lines

    monkeypatch.setattr("imp.cli.session_run.Prompt", _RecordingPrompt)

    coord = InterruptCoordinator()
    make_interactive_solicit(
        quest, status=object(), driver=object(), coord=coord)

    assert seeded["lines"] == ["the seed goal", "a human reply"]


@pytest.mark.anyio
async def test_run_session_client_threads_resume_into_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-resume")
    # On-disk terminal state read after the (stubbed) daemon "exits".
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step=None, step_attempt=0, status="merged").save(
        quest.state_dir)

    captured: dict[str, object] = {}

    def _fake_build(
        ch: Quest, *, resume: bool = False,
        non_interactive: bool = False, autonomous: bool = False,
        lease: bool = False, model: str | None = None,
        opening_unsandboxed: bool = False,
    ) -> Any:
        captured["resume"] = resume
        captured["lease"] = lease

        async def _attach(_on_event: Any) -> Any:  # pragma: no cover - never run
            raise AssertionError("attach must not be invoked in this test")

        return _attach

    monkeypatch.setattr("imp.cli.session_run.build_session_attach", _fake_build)

    async def _noop(**_kwargs: object) -> tuple[StreamEnding, RunWitness]:
        return StreamEnding.CUT, RunWitness()

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self, **_kw: object) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...
        def show_stop_ack(self, _text: str) -> None: ...

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_Driver(), coord=coord, solicit=_solicit, confirm=_no_confirm, resume=True,
        leave_mode="new", stream=_watched())

    assert captured["resume"] is True
    assert captured["lease"] is True   # the terminal path leases
    assert rc == 0



class _NullDriver:
    def on_event(self, _e: dict[str, Any]) -> None: ...
    def finish(self, **_kw: object) -> None: ...
    def seed_pending_reply(self, _text: str) -> None: ...
    def install_startup_status(self) -> None: ...
    def show_stop_ack(self, _text: str) -> None: ...


async def _never(*_a: Any) -> str:
    raise AssertionError("solicit must not be called")


@pytest.mark.anyio
async def test_daemon_startup_timeout_surfaces_distinctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    quest = _make_quest(tmp_path, "feat")
    # Worktree exists in non-completed state → daemon crashed, not fast-completed.
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step="plan", step_attempt=0, status="paused").save(
        quest.state_dir)

    async def _boom(**_kwargs: Any) -> None:
        raise TimeoutError

    monkeypatch.setattr(session_run, "run_terminal_client", _boom)

    coord = InterruptCoordinator()
    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=coord,
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    assert rc == 3                       # distinct daemon-startup-failure code
    out = capsys.readouterr().out
    assert f"resume: imp resume {quest.id}" not in out   # NOT the paused summary
    assert "startup failed: session daemon did not come up" in out   # backstop block on stdout
    assert str(project_state_dir(quest.control_root) / "remote-control.log") in out


@pytest.mark.anyio
async def test_daemon_startup_timeout_is_not_rc3_when_change_already_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    quest = _make_quest(tmp_path, "fast-feat")
    # State is completed but no worktree: daemon finished before the client connected.
    State(id=quest.id, step=None, step_attempt=0, status="merged").save(
        quest.state_dir)

    async def _boom(**_kwargs: Any) -> None:
        raise TimeoutError

    monkeypatch.setattr(session_run, "run_terminal_client", _boom)

    coord = InterruptCoordinator()
    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=coord,
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    assert rc == 0                       # NOT rc=3: change already completed
    captured = capsys.readouterr()
    assert "startup failed" not in (captured.out + captured.err)


@pytest.mark.anyio
async def test_daemon_startup_timeout_is_not_rc3_when_home_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    # The run merged and its home was reaped before the client could attach:
    # the startup-failure check must take its answer from the archive record.
    quest = _archive_reaped_quest(
        tmp_path, "2026-07-03-fastmerge", status="merged", events=_MERGED_EVENT_LINE)

    async def _boom(**_kwargs: Any) -> None:
        raise TimeoutError

    monkeypatch.setattr(session_run, "run_terminal_client", _boom)

    coord = InterruptCoordinator()
    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=coord,
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    captured = capsys.readouterr()
    assert rc == 0                                    # NOT rc=3: the run had completed
    assert "startup failed" not in (captured.out + captured.err)
    assert "merged into main" in captured.out         # rendered from the archived record's status


@pytest.mark.anyio
async def test_run_session_client_forwards_new_leave_mode_to_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    quest = Quest.create_draft(
        tmp_path, title="Draft", quest_id="d1", base_branch="main")
    quest.event_log().append(
        agent_id=None, type="user_message",
        payload={"role": "user", "content": "hi"})

    captured: dict[str, object] = {}

    async def _capture(**kwargs: Any) -> tuple[StreamEnding, RunWitness]:
        captured.update(kwargs)
        return StreamEnding.CUT, RunWitness()

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _capture)

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_NullDriver(), coord=coord,
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())

    assert captured["leave_mode"] == "new"
    # The foreground does NOT apply the disposition — the daemon owns it.
    assert (project_state_dir(tmp_path) / "quests" / "d1").exists()
    assert rc == 4          # cut stream, nothing witnessed, a draft that never merged


@pytest.mark.anyio
async def test_run_session_client_forwards_chat_leave_mode_to_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    quest = Quest.create_draft(
        tmp_path, title="Draft", quest_id="c1", base_branch="main")
    quest.event_log().append(
        agent_id=None, type="user_message",
        payload={"role": "user", "subtype": "intent",
                 "content": [{"type": "text", "text": "a question"}]})

    captured: dict[str, object] = {}

    async def _capture(**kwargs: Any) -> tuple[StreamEnding, RunWitness]:
        captured.update(kwargs)
        return StreamEnding.CUT, RunWitness()

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _capture)

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_NullDriver(), coord=coord,
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="chat", stream=_watched())

    assert captured["leave_mode"] == "chat"
    assert (project_state_dir(tmp_path) / "quests" / "c1").exists()
    assert rc == 4          # cut stream, nothing witnessed, a draft that never merged


@pytest.mark.anyio
async def test_run_session_client_passes_terminal_title_from_quest_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.cli.terminal_title import TerminalTitle

    quest = _make_quest(tmp_path, "2026-06-15-title")   # _make_quest sets title="T"
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step=None, step_attempt=0, status="merged").save(
        quest.state_dir)

    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> tuple[StreamEnding, RunWitness]:
        captured.update(kwargs)
        return StreamEnding.CUT, RunWitness()

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _capture)

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_NullDriver(), coord=coord, solicit=lambda _p: _never(), confirm=_no_confirm,
        leave_mode="new", stream=_watched())

    title = captured["title"]
    assert isinstance(title, TerminalTitle)
    assert title._name == "T"
    assert title._quest_id == "2026-06-15-title"
    assert title._project == quest.control_root.name == tmp_path.name
    assert rc == 0


@pytest.mark.anyio
async def test_run_session_client_passes_control_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-cr")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step=None, step_attempt=0, status="merged").save(
        quest.state_dir)

    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> tuple[StreamEnding, RunWitness]:
        captured.update(kwargs)
        return StreamEnding.CUT, RunWitness()

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _capture)

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_NullDriver(), coord=coord, solicit=lambda _p: _never(), confirm=_no_confirm,
        leave_mode="new", stream=_watched())

    assert captured["control_root"] == quest.control_root
    assert rc == 0


@pytest.mark.anyio
async def test_run_session_client_resets_title_on_daemon_startup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Worktree exists + non-completed state → the resolved outcome is not completed,
    # so this is the rc=3 daemon-startup-failure path (not a fast-completed run).
    quest = _make_quest(tmp_path, "2026-06-15-leak")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step="plan", step_attempt=0, status="paused").save(
        quest.state_dir)

    reset_calls: list[str] = []

    class _FakeTitle:
        def __init__(self, stream: object, quest_id: str, quest_name: str,
                     *, project: str = "") -> None:
            self._quest_id = quest_id
            self._name = quest_name

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

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_NullDriver(), coord=coord, solicit=lambda _p: _never(), confirm=_no_confirm,
        leave_mode="new", stream=_watched())

    assert rc == 3
    assert reset_calls == ["reset"]


@pytest.mark.anyio
async def test_run_session_client_threads_provided_leave_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-ls")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step=None, step_attempt=0, status="merged").save(
        quest.state_dir)

    async def _capture(**kwargs: object) -> tuple[StreamEnding, RunWitness]:
        kwargs["leave_state"]["intent"] = "clear"  # gate-loop populates the dict
        return StreamEnding.CUT, RunWitness()

    seen: dict[str, object] = {}

    def _fake_disposition(
        quest: object, leave_state: object, *, intent: str | None = None,
    ) -> None:
        seen["leave_state"] = leave_state
        return None

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _capture)
    monkeypatch.setattr(
        "imp.cli.main._print_leave_disposition", _fake_disposition)

    coord = InterruptCoordinator()
    ls: dict[str, str] = {}
    rc = await run_session_client(
        quest, driver=_NullDriver(), coord=coord, solicit=lambda _p: _never(), confirm=_no_confirm,
        leave_state=ls, leave_mode="new", stream=_watched())

    assert ls["intent"] == "clear"        # the caller's dict surfaced the signal
    assert seen["leave_state"] is ls      # same dict fed to the disposition
    assert rc == 0


@pytest.mark.anyio
async def test_run_session_client_reuses_passed_in_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A completed change with no worktree → the resolved outcome is completed, rc 0.
    quest = _make_quest(tmp_path, "2026-06-15-reuse")
    State(id=quest.id, step=None, step_attempt=0, status="merged").save(
        quest.state_dir)

    # If run_session_client builds its own title despite one being passed, this fires.
    def _no_construct(*_a: object, **_k: object) -> object:
        raise AssertionError(
            "run_session_client must not construct a title when one is passed")

    monkeypatch.setattr("imp.cli.session_run.TerminalTitle", _no_construct)

    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> tuple[StreamEnding, RunWitness]:
        captured.update(kwargs)
        return StreamEnding.CUT, RunWitness()

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _capture)

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
        quest, driver=_NullDriver(), coord=coord,
        solicit=lambda _p: _never(),
        confirm=_no_confirm, title=sentinel, leave_mode="new", stream=_watched())

    assert captured["title"] is sentinel   # the exact instance flowed through
    assert rc == 0


@pytest.mark.anyio
async def test_run_session_client_threads_autonomous_into_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-aut")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step=None, step_attempt=0, status="merged").save(
        quest.state_dir)

    captured: dict[str, object] = {}

    def _fake_build(
        ch: Quest, *, resume: bool = False,
        non_interactive: bool = False, autonomous: bool = False,
        lease: bool = False, model: str | None = None,
        opening_unsandboxed: bool = False,
    ) -> Any:
        captured["autonomous"] = autonomous

        async def _attach(_on_event: Any) -> Any:  # pragma: no cover - never run
            raise AssertionError("attach must not be invoked in this test")

        return _attach

    monkeypatch.setattr("imp.cli.session_run.build_session_attach", _fake_build)

    async def _noop(**_kwargs: object) -> tuple[StreamEnding, RunWitness]:
        return StreamEnding.CUT, RunWitness()

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self, **_kw: object) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...
        def show_stop_ack(self, _text: str) -> None: ...

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_Driver(), coord=coord, solicit=_solicit,
        confirm=_no_confirm, autonomous=True,
        leave_mode="new", stream=_watched())
    assert rc == 0
    assert captured["autonomous"] is True


@pytest.mark.anyio
async def test_daemon_startup_crash_surfaces_exit_code_and_stderr_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run
    from imp.remote.session.attach import DaemonStartupError

    quest = _make_quest(tmp_path, "crashy")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step="plan", step_attempt=0, status="paused").save(
        quest.state_dir)

    stderr_path = quest.quest_dir / "daemon.stderr"

    async def _boom(**_kwargs: Any) -> None:
        raise DaemonStartupError(
            quest.id, 7, stderr_path, "RuntimeError: boom-on-startup")

    monkeypatch.setattr(session_run, "run_terminal_client", _boom)

    coord = InterruptCoordinator()
    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=coord, solicit=lambda _p: _never(), confirm=_no_confirm,
        leave_mode="new", stream=_watched())

    assert rc == 3
    out = capsys.readouterr().out
    assert "startup failed: session daemon exited with code 7 before it came up" in out
    assert quest.id in out
    assert str(stderr_path) in out
    assert "boom-on-startup" in out


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


_MERGED_EVENT_LINE = (
    b'{"id":"e0","parent_id":null,"agent_id":null,'
    b'"timestamp":"2026-06-01T00:00:01+00:00","type":"quest_merged",'
    b'"payload":{"base":"main","commit":"abc123"}}\n'
)

_INTENT_EVENT_LINE = (
    b'{"id":"e1","parent_id":null,"agent_id":null,'
    b'"timestamp":"2026-06-01T00:00:00+00:00","type":"user_message",'
    b'"payload":{"subtype":"intent",'
    b'"content":[{"type":"text","text":"resolve me from archive"}]}}\n'
)


def _archive_reaped_quest(
    tmp_path: Path, quest_id: str, *, status: str, events: bytes,
) -> Quest:
    """Archive a record for `quest_id`, then return a Quest whose live home was
    never created — the post-merge reaped state the client renders from."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    events_file = tmp_path / f"{quest_id}.jsonl"
    events_file.write_bytes(events)
    quest_json = tmp_path / f"{quest_id}-quest.json"
    quest_json.write_text(json.dumps({
        "id": quest_id, "title": "Reaped", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / f"{quest_id}-state.json"
    state_json.write_text(json.dumps(
        {"id": quest_id, "status": status, "step": None}))
    archive.archive_quest(
        tmp_path, quest_id, events_path=events_file,
        quest_json_path=quest_json, state_json_path=state_json)
    quest = _make_quest(tmp_path, quest_id)
    assert not quest.state_dir.exists()
    return quest


def test_resolve_settled_outcome_falls_back_to_archive_when_home_reaped(
    tmp_path: Path,
) -> None:
    quest = _archive_reaped_quest(
        tmp_path, "2026-07-03-reaped", status="merged", events=_MERGED_EVENT_LINE)
    outcome = resolve_settled_outcome(quest)
    assert outcome.status == "merged"                    # from the archived record
    assert outcome.completed is True
    assert [e["type"] for e in outcome.events] == ["quest_merged"]   # from the archived blob


def test_resolve_settled_outcome_prefers_live_home_over_archive(tmp_path: Path) -> None:
    quest = _archive_reaped_quest(
        tmp_path, "2026-07-03-live", status="merged", events=_MERGED_EVENT_LINE)
    State(id=quest.id, step="plan", step_attempt=0, status="paused").save(
        quest.state_dir)
    quest.event_log().append(
        agent_id=None, type="workflow_paused", payload={"reason": "interrupt"})
    outcome = resolve_settled_outcome(quest)
    assert outcome.status == "paused"                    # live home wins over 'merged'
    assert [e["type"] for e in outcome.events] == ["workflow_paused"]
    assert outcome.completed is False


@pytest.mark.anyio
async def test_run_session_client_wires_seed_pending_reply_as_local_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    quest = Quest.create_draft(
        tmp_path, title="Draft", quest_id="e1", base_branch="main")
    quest.event_log().append(
        agent_id=None, type="user_message",
        payload={"role": "user", "content": "hi"})

    captured: dict[str, object] = {}

    async def _capture(**kwargs: Any) -> tuple[StreamEnding, RunWitness]:
        captured.update(kwargs)
        return StreamEnding.CUT, RunWitness()

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _capture)

    driver = _NullDriver()
    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=driver, coord=coord, solicit=lambda _p: _never(), confirm=_no_confirm,
        leave_mode="new", stream=_watched())

    assert captured["local_echo"] == driver.seed_pending_reply
    assert rc == 4          # cut stream, nothing witnessed, a draft that never merged


@pytest.mark.anyio
async def test_run_session_client_installs_startup_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-startup")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step=None, step_attempt=0, status="merged").save(
        quest.state_dir)

    async def _noop(**_kwargs: object) -> tuple[StreamEnding, RunWitness]:
        return StreamEnding.CUT, RunWitness()

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)

    installed: list[bool] = []

    class _Driver(_NullDriver):
        def install_startup_status(self) -> None:
            installed.append(True)

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_Driver(), coord=coord, solicit=lambda _p: _never(), confirm=_no_confirm,
        leave_mode="new", stream=_watched())

    assert installed == [True]           # called exactly once
    assert rc == 0


@pytest.mark.anyio
async def test_run_session_client_threads_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-model")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step=None, step_attempt=0, status="merged").save(
        quest.state_dir)

    captured: dict[str, object] = {}

    def _fake_build(ch: Any, **kw: Any) -> Any:
        captured.update(kw)

        async def _attach(_on_event: Any) -> Any:  # pragma: no cover
            raise AssertionError("attach must not be invoked in this test")

        return _attach

    monkeypatch.setattr("imp.cli.session_run.build_session_attach", _fake_build)

    async def _noop(**_kwargs: object) -> tuple[StreamEnding, RunWitness]:
        return StreamEnding.CUT, RunWitness()

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)

    class _Driver:
        def on_event(self, _event: Any) -> None: ...
        def finish(self, **_kw: object) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...
        def show_stop_ack(self, _text: str) -> None: ...

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_Driver(), coord=coord, solicit=_solicit, confirm=_no_confirm,
        model="anthropic:claude-fable-5", leave_mode="new", stream=_watched())

    assert rc == 0
    assert captured["model"] == "anthropic:claude-fable-5"


@pytest.mark.anyio
async def test_run_session_client_threads_opening_unsandboxed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-unsbx")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step=None, step_attempt=0, status="merged").save(
        quest.state_dir)

    captured: dict[str, object] = {}

    def _fake_build(ch: Any, **kw: Any) -> Any:
        captured.update(kw)

        async def _attach(_on_event: Any) -> Any:  # pragma: no cover
            raise AssertionError("attach must not be invoked in this test")

        return _attach

    monkeypatch.setattr("imp.cli.session_run.build_session_attach", _fake_build)

    async def _noop(**_kwargs: object) -> tuple[StreamEnding, RunWitness]:
        return StreamEnding.CUT, RunWitness()

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)

    class _Driver:
        def on_event(self, _event: Any) -> None: ...
        def finish(self, **_kw: object) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...
        def show_stop_ack(self, _text: str) -> None: ...

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_Driver(), coord=coord, solicit=_solicit, confirm=_no_confirm,
        opening_unsandboxed=True, leave_mode="new", stream=_watched())

    assert rc == 0
    assert captured["opening_unsandboxed"] is True


def test_run_session_client_leave_mode_is_required() -> None:
    import inspect
    p = inspect.signature(run_session_client).parameters["leave_mode"]
    assert p.default is inspect.Parameter.empty


def test_run_session_client_stream_is_required() -> None:
    # A leased attach that forgot its wrapper would run unwatched; the signature forbids it.
    import inspect
    p = inspect.signature(run_session_client).parameters["stream"]
    assert p.default is inspect.Parameter.empty


@pytest.mark.anyio
async def test_daemon_layout_downgrade_surfaces_message_directly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run
    from imp.remote.session.attach import LAYOUT_MISMATCH_EXIT, DaemonStartupError

    quest = _make_quest(tmp_path, "stale")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step="plan", step_attempt=0, status="paused").save(
        quest.state_dir)

    stderr_path = quest.quest_dir / "daemon.stderr"
    downgrade_msg = (
        "imp: stale's data is at layout v13, newer than this imp (v12) — "
        "this install is older than the data that wrote it. "
        "Update imp to v13 or newer to run this quest.")

    async def _boom(**_kwargs: Any) -> None:
        raise DaemonStartupError(
            quest.id, LAYOUT_MISMATCH_EXIT, stderr_path, downgrade_msg)

    monkeypatch.setattr(session_run, "run_terminal_client", _boom)

    coord = InterruptCoordinator()
    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=coord, solicit=lambda _p: _never(), confirm=_no_confirm,
        leave_mode="new", stream=_watched())

    assert rc == 2                                       # clean user error, not the crash rc 3
    captured = capsys.readouterr()
    assert "Update imp to v13 or newer" in captured.err  # message surfaced directly
    assert "startup failed" not in captured.out          # NOT the generic crash block


@pytest.mark.anyio
async def test_run_session_client_registers_ack_hook_when_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-ack-on")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step=None, step_attempt=0, status="merged").save(
        quest.state_dir)

    async def _noop(**_kwargs: object) -> tuple[StreamEnding, RunWitness]:
        return StreamEnding.CUT, RunWitness()
    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)

    class _Driver:
        def __init__(self) -> None:
            self.acks: list[str] = []
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self, **_kw: object) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...
        def show_stop_ack(self, text: str) -> None:
            self.acks.append(text)

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    drv = _Driver()
    rc = await run_session_client(
        quest, driver=drv, coord=coord, solicit=_solicit, confirm=_no_confirm,
        leave_mode="new", non_interactive=False, stream=_watched())

    assert rc == 0
    assert coord._ack_hook is not None            # hook registered on an interactive run
    coord._ack_hook("stopping…")                  # and it routes to the driver
    assert drv.acks == ["stopping…"]


@pytest.mark.anyio
async def test_run_session_client_leaves_ack_hook_unregistered_when_headless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-ack-off")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step=None, step_attempt=0, status="merged").save(
        quest.state_dir)

    async def _noop(**_kwargs: object) -> tuple[StreamEnding, RunWitness]:
        return StreamEnding.CUT, RunWitness()
    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self, **_kw: object) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...
        def show_stop_ack(self, _text: str) -> None:  # pragma: no cover
            raise AssertionError("headless run must not render an interrupt ack")

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_Driver(), coord=coord, solicit=_solicit, confirm=_no_confirm,
        leave_mode="new", non_interactive=True, stream=_watched())

    assert rc == 0
    assert coord._ack_hook is None                # never registered in a headless run


_cut = _ending_with(StreamEnding.CUT)
_ended = _ending_with(StreamEnding.ENDED)


def _leaving(reason: str | None = None, *,
             witnessed: tuple[dict[str, Any], ...] = (), **marks: str) -> Any:
    """A CUT ending from a deliberate leave: bind and set the escalation event the way
    session_terminal.py does, record the marks the gesture itself would leave, and fold
    whatever the client saw before it departed."""
    async def _stub(**kwargs: Any) -> tuple[StreamEnding, RunWitness]:
        coord = kwargs["coord"]
        coord.escalation_event = anyio.Event()
        coord.escalation_event.set()
        coord.leave_reason = reason
        kwargs["leave_state"].update(marks)
        witness = RunWitness()
        for event in witnessed:
            witness.note(event)
        return StreamEnding.CUT, witness
    return _stub


async def _abandoning(**kwargs: Any) -> tuple[StreamEnding, RunWitness]:
    # /abandon records its intent and sends its own control frame; it deliberately
    # never sets the escalation event (see session_terminal.py).
    kwargs["leave_state"]["intent"] = "abandon"
    return StreamEnding.CUT, RunWitness()


@pytest.mark.anyio
async def test_cut_ending_with_merged_state_renders_the_completed_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    # The daemon finished in the milliseconds after the cut (the behi75 case).
    quest = _make_quest(tmp_path, "2026-07-04-cut-merged")
    State(id=quest.id, step=None, step_attempt=0, status="merged").save(
        quest.state_dir)
    quest.event_log().append(agent_id=None, type="quest_merged", payload={})
    monkeypatch.setattr(session_run, "run_terminal_client", _cut)

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 0
    assert out.split("\n")[0] == f"{quest.id} · T · merged into main"
    assert "lost the connection" not in out


@pytest.mark.anyio
async def test_cut_ending_on_a_stale_paused_quest_reports_lost_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _make_quest(tmp_path, "2026-07-04-cut-stale")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step="plan", step_attempt=0, status="paused").save(
        quest.state_dir)
    monkeypatch.setattr(session_run, "run_terminal_client", _cut)

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    lines = out.split("\n")
    assert rc == 4
    assert lines[0] == "lost the connection to the run"
    assert lines[1] == (
        f"It may still be running — `imp resume {quest.id}` to reattach.")
    assert lines[2] == f"{quest.id} · T"
    assert out.endswith("\n\n") and not out.endswith("\n\n\n")
    assert "paused" not in out
    assert f"resume: imp resume {quest.id}" not in out


@pytest.mark.anyio
async def test_end_frame_with_a_genuine_pause_renders_the_pause_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _make_quest(tmp_path, "2026-07-04-ended-paused")
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _ending_with(StreamEnding.ENDED,
                     {"type": "workflow_paused", "payload": {"reason": "awaiting_user"}}))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 130
    assert out.split("\n")[0] == "paused"
    assert f"resume: imp resume {quest.id}" in out
    assert "lost the connection" not in out


def test_resolve_settled_outcome_ignores_a_stray_live_event_line_when_home_reaped(
    tmp_path: Path,
) -> None:
    # The keku73 shape: the home was reaped, then a later attach wrote one line back.
    quest = _archive_reaped_quest(
        tmp_path, "2026-07-05-stray", status="merged",
        events=_INTENT_EVENT_LINE + _MERGED_EVENT_LINE)
    quest.event_log().append(agent_id=None, type="cli_attached", payload={})
    assert not quest.state_dir.exists()          # only events.jsonl came back
    outcome = resolve_settled_outcome(quest)
    assert outcome.status == "merged"
    # state.json does not load, so BOTH fields come from the archive; the stray
    # live line never shadows the archived blob.
    assert [e["type"] for e in outcome.events] == ["user_message", "quest_merged"]


def test_resolve_settled_outcome_never_takes_events_from_the_archive_when_home_is_live(
    tmp_path: Path,
) -> None:
    quest = _archive_reaped_quest(
        tmp_path, "2026-07-05-live", status="merged",
        events=_INTENT_EVENT_LINE + _MERGED_EVENT_LINE)
    State(id=quest.id, step="plan", step_attempt=0, status="paused").save(
        quest.state_dir)
    outcome = resolve_settled_outcome(quest)
    # state.json loads, so the home answers both fields — an empty live event log
    # is an answer, not a reason to reach into the archive.
    assert outcome.status == "paused"
    assert outcome.events == []
    assert outcome.completed is False


@pytest.mark.anyio
async def test_watched_stream_reaches_the_fallback_title_and_the_terminal_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.session_run as session_run

    quest = _make_quest(tmp_path, "watched-feat")
    State(id=quest.id, step="plan", step_attempt=0, status="paused").save(
        quest.state_dir)
    watched = WatchedStream(io.StringIO())
    seen: dict[str, Any] = {}

    class _RecordingTitle:
        def __init__(self, stream: Any, *_a: Any, **_kw: Any) -> None:
            seen["title_stream"] = stream

        def reset(self) -> None: ...

    async def _stub(**kwargs: Any) -> tuple[StreamEnding, RunWitness]:
        seen["client_stream"] = kwargs["stream"]
        seen["title"] = kwargs["title"]
        return StreamEnding.ENDED, RunWitness()

    monkeypatch.setattr(session_run, "TerminalTitle", _RecordingTitle)
    monkeypatch.setattr(session_run, "run_terminal_client", _stub)

    coord = InterruptCoordinator()
    await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=coord,
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=watched)

    # The fallback TerminalTitle and run_terminal_client hold the SAME wrapper.
    assert seen["title_stream"] is watched
    assert seen["client_stream"] is watched
    assert isinstance(seen["title"], _RecordingTitle)


@pytest.mark.anyio
async def test_cut_ending_on_a_live_run_reports_lost_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    # The dobo95 shape: the client left the stream while the run was still going.
    quest = _make_quest(tmp_path, "2026-07-04-cut-live")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step="merge-to-base", step_attempt=0, status="new").save(
        quest.state_dir)
    monkeypatch.setattr(session_run, "run_terminal_client", _cut)

    coord = InterruptCoordinator()
    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=coord,
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    lines = out.split("\n")
    assert coord.escalation_event is None        # no deliberate-leave mark was set
    assert rc == 4
    assert lines[0] == "lost the connection to the run"
    assert lines[1] == (
        f"It may still be running — `imp resume {quest.id}` to reattach.")
    assert lines[2] == f"{quest.id} · T"
    assert "paused" not in out
    assert f"resume: imp resume {quest.id}" not in out


@pytest.mark.anyio
async def test_cut_ending_with_no_resolvable_status_reports_lost_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    # Neither the reaped home nor the archive can answer: still never a pause summary.
    quest = _make_quest(tmp_path, "2026-07-04-cut-unresolvable")
    assert not quest.state_dir.exists()
    monkeypatch.setattr(session_run, "run_terminal_client", _cut)

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 4
    assert out.split("\n")[0] == "lost the connection to the run"
    assert "paused" not in out


@pytest.mark.anyio
async def test_end_frame_with_done_state_renders_the_completed_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _make_quest(tmp_path, "2026-07-04-ended-done")
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _ending_with(StreamEnding.ENDED, {"type": "workflow_completed", "payload": {}}))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 0
    assert out.split("\n")[0] == f"{quest.id} · T · done · commits on imp/{quest.id}"
    assert "lost the connection" not in out


@pytest.mark.anyio
async def test_end_frame_with_merged_state_renders_the_completed_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _make_quest(tmp_path, "2026-07-04-ended-merged")
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _ending_with(StreamEnding.ENDED,
                     {"type": "quest_merged",
                      "payload": {"base": "main", "commit": "abc123"}}))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 0
    assert out.split("\n")[0] == f"{quest.id} · T · merged into main"
    assert "lost the connection" not in out


@pytest.mark.anyio
async def test_end_frame_with_merge_blocked_state_renders_the_merge_blocked_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _make_quest(tmp_path, "2026-07-04-ended-blocked")
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _ending_with(StreamEnding.ENDED,
                     {"type": "merge_blocked",
                      "payload": {"reason": "conflict", "base": "main"}}))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 1
    assert out.split("\n")[0] == (
        f"merge blocked: imp/{quest.id} doesn't auto-merge into main")
    assert "lost the connection" not in out


def _paused_quest(tmp_path: Path, quest_id: str) -> Quest:
    quest = _make_quest(tmp_path, quest_id)
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step="plan", step_attempt=0, status="paused").save(
        quest.state_dir)
    return quest


def _ended_by(leave: str, *events: dict[str, Any], **marks: str) -> Any:
    """An ENDED ending whose end frame named `leave`, after the witnessed events; `marks`
    are this client's own leave_state entries."""
    async def _stub(**kwargs: Any) -> tuple[StreamEnding, RunWitness]:
        kwargs["leave_state"].update(marks)
        witness = RunWitness()
        for event in events:
            witness.note(event)
        witness.note_end(EndOfRun(leave))
        return StreamEnding.ENDED, witness
    return _stub


@pytest.mark.anyio
@pytest.mark.parametrize(("disposition", "tail"), [
    ("archive", "archived\nresume: imp resume {id} — or find it later with imp search\n"),
    ("discard", "discarded\n"),
    ("save", "paused\nresume: imp resume {id}\n"),
    ("abandon", "abandoned\n"),
])
async def test_an_end_frame_disposition_renders_that_leaves_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], disposition: str, tail: str,
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    # Disk says `paused` and nothing was witnessed: without the disposition this is
    # the daemon-died block.
    quest = _paused_quest(tmp_path, f"2026-09-22-end-{disposition}")
    monkeypatch.setattr(session_run, "run_terminal_client", _ended_by(disposition))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 130
    assert out == f"{quest.id} · T · " + tail.format(id=quest.id) + "\n"


@pytest.mark.anyio
async def test_an_end_frame_disposition_outranks_a_witnessed_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _paused_quest(tmp_path, "2026-09-22-end-over-pause")
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _ended_by("abandon",
                  {"type": "workflow_paused", "payload": {"reason": "max_loops"}}))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 130
    assert out == f"{quest.id} · T · abandoned\n\n"


@pytest.mark.anyio
async def test_the_clients_own_leave_outranks_the_end_frame_disposition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _paused_quest(tmp_path, "2026-09-22-own-leave-first")
    monkeypatch.setattr(
        session_run, "run_terminal_client", _ended_by("save", disposition="archive"))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 130
    assert out == (f"{quest.id} · T · archived\n"
                   f"resume: imp resume {quest.id} — or find it later with imp search\n\n")


@pytest.mark.anyio
async def test_ctrl_d_leave_still_renders_its_pause_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _paused_quest(tmp_path, "2026-07-04-leave-ctrl-d")
    monkeypatch.setattr(
        session_run, "run_terminal_client", _leaving("ctrl-d"))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 130
    # The WHOLE buffer, not a prefix: AC 5 asks for byte-identical leave rendering.
    assert out == (
        "> Ctrl-D\n"                       # the caret shadow of the keystroke
        "paused · Ctrl-D\n"
        f"{quest.id} · T\n"
        f"resume: imp resume {quest.id}\n"
        "\n")


@pytest.mark.anyio
async def test_sigterm_leave_still_renders_its_pause_block_without_a_caret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _paused_quest(tmp_path, "2026-07-04-leave-sigterm")
    monkeypatch.setattr(
        session_run, "run_terminal_client", _leaving("sigterm"))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 130
    assert out == (
        "paused · SIGTERM\n"               # a signal typed nothing: no caret shadow
        f"{quest.id} · T\n"
        f"resume: imp resume {quest.id}\n"
        "\n")


@pytest.mark.anyio
async def test_save_leave_still_renders_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _paused_quest(tmp_path, "2026-07-04-leave-save")
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _leaving(None, intent="save", disposition="save"))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 130
    assert out == (
        f"{quest.id} · T · paused\n"
        f"resume: imp resume {quest.id}\n"
        "\n")


@pytest.mark.anyio
async def test_a_keystroke_leave_with_a_disposition_renders_only_the_draft_spine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _paused_quest(tmp_path, "2026-07-04-leave-save-ctrl-d")
    # A Ctrl-D leave the gate loop resolved to `save`: the disposition mark carries no
    # intent, so a row 1 keyed on intent alone would fall through to the trigger row.
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _leaving("ctrl-d", disposition="save"))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 130
    assert out == (
        f"{quest.id} · T · paused\n"
        f"resume: imp resume {quest.id}\n"
        "\n")
    assert "> Ctrl-D" not in out          # row 1 prints no caret shadow
    assert "paused · Ctrl-D" not in out   # …and no trigger head


@pytest.mark.anyio
async def test_pause_on_a_built_quest_renders_the_trigger_block_without_a_caret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _paused_quest(tmp_path, "2026-07-04-leave-pause-built")
    # A built quest's /pause: the gesture sets the trigger, and no disposition is sent.
    monkeypatch.setattr(
        session_run, "run_terminal_client", _leaving("pause", intent="pause"))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 130
    assert out == (
        "paused · /pause\n"
        f"{quest.id} · T\n"
        f"resume: imp resume {quest.id}\n"
        "\n")


@pytest.mark.anyio
async def test_shelve_leave_still_renders_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _paused_quest(tmp_path, "2026-07-04-leave-shelve")
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _leaving(None, intent="shelve", disposition="archive"))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 130
    assert out == (
        f"{quest.id} · T · archived\n"
        f"resume: imp resume {quest.id} — or find it later with imp search\n"
        "\n")


@pytest.mark.anyio
async def test_headless_gate_detach_still_renders_the_bare_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    # The headless solicit trips escalation with no leave_reason and no intent.
    quest = _paused_quest(tmp_path, "2026-07-04-leave-headless")
    monkeypatch.setattr(session_run, "run_terminal_client", _leaving(None))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new",
        non_interactive=True, stream=_watched())
    out = capsys.readouterr().out
    assert rc == 130
    # No leave_reason, so no qualifier: the bare pause block, exactly as today.
    assert out == (
        "paused\n"
        f"{quest.id} · T\n"
        f"resume: imp resume {quest.id}\n"
        "\n")


@pytest.mark.anyio
async def test_abandon_renders_abandoned_with_the_escalation_event_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _make_quest(tmp_path, "2026-07-04-leave-abandon")
    State(id=quest.id, step="plan", step_attempt=0, status="abandoned").save(
        quest.state_dir)
    monkeypatch.setattr(session_run, "run_terminal_client", _abandoning)

    coord = InterruptCoordinator()
    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=coord,
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    # The leave_state arm carries this one: escalation is never set for /abandon.
    assert coord.escalation_event is None
    assert rc == 130
    assert out == f"{quest.id} · T · abandoned\n\n"      # no resume line for an abandon


@pytest.mark.anyio
async def test_witnessed_merge_renders_merged_with_no_quest_home_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    # The gidi44 shape: the merge is witnessed, then the home is reaped and the
    # archive cannot answer. Disk has nothing to offer and must not be consulted.
    quest = _make_quest(tmp_path, "2026-09-20-witnessed-merge")
    quest.title = ""
    assert not quest.state_dir.exists()
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _ending_with(
            StreamEnding.ENDED,
            {"type": "user_message",
             "payload": {"subtype": "intent",
                         "content": [{"type": "text", "text": "fold the corrections"}]}},
            {"type": "quest_merged",
             "payload": {"base": "master", "commit": "20d0d699"}}))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 0
    # The witnessed payload names the base (the quest's own attribute is `main`),
    # and the identity line carries the witnessed opening prompt.
    assert out == f"{quest.id} · fold the corrections · merged into master\n\n"
    assert "paused" not in out
    assert "resume: imp resume" not in out


@pytest.mark.anyio
async def test_witnessed_completion_renders_done_with_no_quest_home_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _make_quest(tmp_path, "2026-09-20-witnessed-done")
    assert not quest.state_dir.exists()
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _ending_with(StreamEnding.ENDED, {"type": "workflow_completed", "payload": {}}))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 0
    assert out == f"{quest.id} · T · done · commits on imp/{quest.id}\n\n"


@pytest.mark.anyio
async def test_witnessed_merge_blocked_renders_the_payloads_reason_and_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _make_quest(tmp_path, "2026-09-20-witnessed-blocked")
    assert not quest.state_dir.exists()
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _ending_with(
            StreamEnding.ENDED,
            {"type": "merge_blocked",
             "payload": {"reason": "conflict", "base": "main",
                         "detail": "CONFLICT in a.py",
                         "note": "imp attempted 3 recovery passes."}}))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    lines = out.split("\n")
    assert rc == 1
    assert lines[0] == f"merge blocked: imp/{quest.id} doesn't auto-merge into main"
    assert "imp attempted 3 recovery passes." in out      # the witnessed note
    assert "git said: CONFLICT in a.py" in out            # the witnessed detail
    assert not any(ln.startswith("resume: imp resume") for ln in lines)


@pytest.mark.anyio
async def test_witnessed_awaiting_user_pause_renders_the_bare_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    # A your-turn park: its reason folds to nothing, so only a row keyed on the
    # witnessing itself can render it. The log on disk holds a louder fossil, to
    # prove the block comes from the witness and not from that read.
    quest = _make_quest(tmp_path, "2026-09-20-awaiting")
    quest.event_log().append(
        agent_id=None, type="workflow_paused", payload={"reason": "max_loops"})
    assert not quest.state_dir.exists()
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _ending_with(StreamEnding.ENDED,
                     {"type": "workflow_paused", "payload": {"reason": "awaiting_user"}}))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 130
    assert out == (
        "paused\n"
        f"{quest.id} · T\n"
        f"resume: imp resume {quest.id}\n"
        "\n")
    assert "loop limit" not in out               # the disk fossil never surfaced


@pytest.mark.anyio
async def test_witnessed_loop_limit_pause_renders_the_qualified_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _make_quest(tmp_path, "2026-09-20-loop-limit")
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _ending_with(StreamEnding.ENDED,
                     {"type": "workflow_paused", "payload": {"reason": "max_loops"}}))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 1
    assert out == (
        "paused · loop limit\n"
        f"{quest.id} · T\n"
        f"resume: imp resume {quest.id}\n"
        "\n")


@pytest.mark.anyio
async def test_witnessed_error_pause_renders_the_error_reason_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _make_quest(tmp_path, "2026-09-20-api-error")
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _ending_with(
            StreamEnding.ENDED,
            {"type": "error", "payload": {
                "detail": {"last_agent_text": "overloaded_error\nretry later"}}},
            {"type": "workflow_paused", "payload": {"reason": "api"}}))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 1
    assert out == (
        "API error: overloaded_error\n"
        "retry later\n"
        f"{quest.id} · T\n"
        "\n")
    assert "resume: imp resume" not in out       # the error block carries none


@pytest.mark.anyio
async def test_a_witnessed_outcome_preempts_a_keystroke_leave_and_prints_no_caret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    # The arm that produced gidi44: a leave mark used to divert a merged run into
    # the disk read. The witnessed merge outranks it, and prints no caret.
    quest = _paused_quest(tmp_path, "2026-09-20-leave-after-merge")
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _leaving("ctrl-d", witnessed=(
            {"type": "quest_merged",
             "payload": {"base": "main", "commit": "abc123"}},)))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 0
    assert out == f"{quest.id} · T · merged into main\n\n"
    assert "> Ctrl-D" not in out
    assert "paused" not in out


@pytest.mark.anyio
async def test_a_witnessed_pause_never_outranks_a_deliberate_leave_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    # The daemon writes its own pause after this client departs, so the client's
    # trigger is the fresher truth: the leave row must sit above the pause row.
    quest = _paused_quest(tmp_path, "2026-09-20-leave-over-pause")
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _leaving("ctrl-d", witnessed=(
            {"type": "workflow_paused", "payload": {"reason": "max_loops"}},)))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 130
    assert out == (
        "> Ctrl-D\n"
        "paused · Ctrl-D\n"
        f"{quest.id} · T\n"
        f"resume: imp resume {quest.id}\n"
        "\n")
    assert "loop limit" not in out


@pytest.mark.anyio
async def test_headless_detach_on_a_resumed_quest_renders_the_bare_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    # The headless gate detach on a resumed quest. Its log ends on workflow_resumed
    # with no later pause — the shape today's STALE limb turns into an `interrupt`
    # head. The witness saw the same two events and forgot the pause on the resume.
    quest = _paused_quest(tmp_path, "2026-09-20-detach-resumed")
    quest.event_log().append(
        agent_id=None, type="workflow_paused", payload={"reason": "api"})
    quest.event_log().append(agent_id=None, type="workflow_resumed", payload={})
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _leaving(None, witnessed=(
            {"type": "workflow_paused", "_replay": True, "payload": {"reason": "api"}},
            {"type": "workflow_resumed", "_replay": True, "payload": {}})))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new",
        non_interactive=True, stream=_watched())
    out = capsys.readouterr().out
    assert rc == 130
    assert out == (
        "paused\n"
        f"{quest.id} · T\n"
        f"resume: imp resume {quest.id}\n"
        "\n")
    assert "interrupt" not in out        # the inferred STALE head is retired
    assert "API error" not in out


@pytest.mark.anyio
async def test_a_resumed_merge_blocked_never_outranks_this_segments_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _make_quest(tmp_path, "2026-09-20-replay-blocked")
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _ending_with(
            StreamEnding.ENDED,
            {"type": "merge_blocked", "_replay": True,
             "payload": {"reason": "conflict", "base": "main"}},
            {"type": "workflow_resumed", "_replay": True, "payload": {}},
            {"type": "workflow_paused", "payload": {"reason": "max_loops"}}))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 1
    assert out.split("\n")[0] == "paused · loop limit"
    assert "merge blocked" not in out


@pytest.mark.anyio
async def test_a_resumed_merge_blocked_then_a_merge_renders_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _make_quest(tmp_path, "2026-09-20-replay-then-merge")
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _ending_with(
            StreamEnding.ENDED,
            {"type": "merge_blocked", "_replay": True,
             "payload": {"reason": "conflict", "base": "main"}},
            {"type": "workflow_resumed", "_replay": True, "payload": {}},
            {"type": "quest_merged", "payload": {"base": "main", "commit": "abc123"}}))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 0
    assert out.split("\n")[0] == f"{quest.id} · T · merged into main"
    assert "merge blocked" not in out


@pytest.mark.anyio
async def test_end_frame_with_nothing_witnessed_reports_a_crash_when_stderr_has_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _paused_quest(tmp_path, "2026-09-20-died-crash")
    stderr_path = quest.quest_dir / "daemon.stderr"
    stderr_path.write_text("Traceback (most recent call last):\nRuntimeError: boom\n")
    monkeypatch.setattr(session_run, "run_terminal_client", _ended)

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 1
    assert out == (
        f"crash: see {stderr_path}\n"
        f"{quest.id} · T\n"
        "\n")


@pytest.mark.anyio
async def test_end_frame_with_nothing_witnessed_reports_no_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    # The BaseException gap: the run emitted nothing, the server still sent its
    # end frame. Disk says `paused`, and must not be believed.
    quest = _paused_quest(tmp_path, "2026-09-20-died-silent")
    monkeypatch.setattr(session_run, "run_terminal_client", _ended)

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 1
    assert out == (
        "run ended without reporting an outcome\n"
        f"It may not have finished — `imp resume {quest.id}` to check.\n"
        f"{quest.id} · T\n"
        "\n")
    assert not any(ln.startswith("resume: imp resume") for ln in out.split("\n"))
    assert "paused" not in out


@pytest.mark.anyio
async def test_a_replayed_pause_then_resume_then_a_cut_reports_lost_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _paused_quest(tmp_path, "2026-09-20-replay-fossil")
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _ending_with(
            StreamEnding.CUT,
            {"type": "workflow_paused", "_replay": True, "payload": {"reason": "api"}},
            {"type": "workflow_resumed", "_replay": True, "payload": {}}))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 4
    assert out.split("\n")[0] == "lost the connection to the run"
    assert "API error" not in out                # the fossil reason is gone
    assert "paused" not in out


@pytest.mark.anyio
async def test_a_replayed_pause_then_resume_then_an_end_frame_reports_the_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _paused_quest(tmp_path, "2026-09-20-replay-crash")
    stderr_path = quest.quest_dir / "daemon.stderr"
    stderr_path.write_text("Traceback (most recent call last):\nLoadError\n")
    monkeypatch.setattr(
        session_run, "run_terminal_client",
        _ending_with(
            StreamEnding.ENDED,
            {"type": "workflow_paused", "_replay": True, "payload": {"reason": "api"}},
            {"type": "workflow_resumed", "_replay": True, "payload": {}}))

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), confirm=_no_confirm, leave_mode="new", stream=_watched())
    out = capsys.readouterr().out
    assert rc == 1
    assert out == (
        f"crash: see {stderr_path}\n"
        f"{quest.id} · T\n"
        "\n")
