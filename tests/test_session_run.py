from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from imp.cli import main
from imp.cli.session_run import (
    make_headless_solicit,
    make_interactive_solicit,
    resolve_settled_outcome,
    run_session_client,
)
from imp.cli.signals import InterruptCoordinator
from imp.engine import archive
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.paths import project_state_dir
from imp.remote.session.client import StreamEnding
from imp.remote.session.discover import session_socket_path
from imp.remote.session.server import SessionServer
from tests.async_wait import HANG_BACKSTOP, wait_until
from tests.conftest import _seed_control


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
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-run")
    # The on-disk terminal state the client reads once the daemon exits.
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step=None, step_attempt=0, status="merged").save(
        quest.state_dir)

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
        return "merged"

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...
        def show_stop_ack(self, _text: str) -> None: ...

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
                    quest, driver=_Driver(), coord=coord, solicit=_solicit,
                    leave_mode="new")

            tg.start_soon(_go)
            await wait_until(lambda: "rc" in rc_holder)
            tg.cancel_scope.cancel()

    assert answered == ["imp"]
    assert rc_holder["rc"] == 0


@pytest.mark.anyio
async def test_run_session_client_starts_status_tick_when_provided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-tick")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step=None, step_attempt=0, status="merged").save(
        quest.state_dir)

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
        def finish(self) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...
        def show_stop_ack(self, _text: str) -> None: ...

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await wait_until(lambda: server.attached_count != 0)
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
                    solicit=_solicit, status=_Status(), leave_mode="new")

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
    # Worktree exists + paused status → the resolved outcome is not completed.
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step="plan", step_attempt=0, status="paused").save(
        quest.state_dir)

    async def _noop(**_kwargs: object) -> None:
        pass

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)
    monkeypatch.setattr("imp.cli.main._handle_terminal_outcome", lambda *_: 1)

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...
        def show_stop_ack(self, _text: str) -> None: ...

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    # The durable `interrupted` flag (set by signal_pause on a real Ctrl-C) is what
    # _session_exit_code reads for 130 — set it ALONE (should_pause stays False) so
    # this test fails if the exit-code guard regresses to reading should_pause.
    coord._interrupted = True
    rc = await run_session_client(
        quest, driver=_Driver(), coord=coord, solicit=_solicit,
        leave_mode="new")
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

    async def _noop(**_kwargs: object) -> None:
        pass

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...
        def show_stop_ack(self, _text: str) -> None: ...

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_Driver(), coord=coord, solicit=_solicit, resume=True,
        leave_mode="new")

    assert captured["resume"] is True
    assert captured["lease"] is True   # the terminal path leases
    assert rc == 0



class _NullDriver:
    def on_event(self, _e: dict[str, Any]) -> None: ...
    def finish(self) -> None: ...
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
    summarized: list[Any] = []
    monkeypatch.setattr(
        "imp.cli.main._handle_terminal_outcome",
        lambda c, r, co: summarized.append(r) or 1)

    coord = InterruptCoordinator()
    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=coord,
        solicit=lambda _p: _never(), leave_mode="new")
    assert rc == 3                       # distinct daemon-startup-failure code
    assert summarized == []              # NOT routed through the paused summary
    out = capsys.readouterr().out
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
    monkeypatch.setattr(
        "imp.cli.main._handle_terminal_outcome",
        lambda c, r, co: 0)

    coord = InterruptCoordinator()
    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=coord,
        solicit=lambda _p: _never(), leave_mode="new")
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
        solicit=lambda _p: _never(), leave_mode="new")
    captured = capsys.readouterr()
    assert rc == 0                                    # NOT rc=3: the run had completed
    assert "startup failed" not in (captured.out + captured.err)
    assert "merged into main" in captured.out         # rendered through the resolver's events


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

    async def _capture(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _capture)
    monkeypatch.setattr("imp.cli.main._session_exit_code", lambda *_: 0)

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_NullDriver(), coord=coord,
        solicit=lambda _p: _never(), leave_mode="new")

    assert captured["leave_mode"] == "new"
    # The foreground does NOT apply the disposition — the daemon owns it.
    assert (project_state_dir(tmp_path) / "quests" / "d1").exists()
    assert rc == 0


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

    async def _capture(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _capture)
    monkeypatch.setattr("imp.cli.main._session_exit_code", lambda *_: 0)

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_NullDriver(), coord=coord,
        solicit=lambda _p: _never(), leave_mode="chat")

    assert captured["leave_mode"] == "chat"
    assert (project_state_dir(tmp_path) / "quests" / "c1").exists()
    assert rc == 0


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

    async def _capture(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _capture)
    monkeypatch.setattr("imp.cli.main._handle_terminal_outcome", lambda *_: 0)

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_NullDriver(), coord=coord, solicit=lambda _p: _never(),
        leave_mode="new")

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

    async def _capture(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _capture)
    monkeypatch.setattr("imp.cli.main._handle_terminal_outcome", lambda *_: 0)

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_NullDriver(), coord=coord, solicit=lambda _p: _never(),
        leave_mode="new")

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
    monkeypatch.setattr("imp.cli.main._handle_terminal_outcome", lambda *_: 1)

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_NullDriver(), coord=coord, solicit=lambda _p: _never(),
        leave_mode="new")

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

    async def _capture(**kwargs: object) -> None:
        kwargs["leave_state"]["intent"] = "clear"  # gate-loop populates the dict

    seen: dict[str, object] = {}

    def _fake_disposition(quest: object, leave_state: object) -> None:
        seen["leave_state"] = leave_state
        return None

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _capture)
    monkeypatch.setattr(
        "imp.cli.main._print_leave_disposition", _fake_disposition)
    monkeypatch.setattr("imp.cli.main._session_exit_code", lambda *_: 0)

    coord = InterruptCoordinator()
    ls: dict[str, str] = {}
    rc = await run_session_client(
        quest, driver=_NullDriver(), coord=coord, solicit=lambda _p: _never(),
        leave_state=ls, leave_mode="new")

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
        quest, driver=_NullDriver(), coord=coord,
        solicit=lambda _p: _never(), title=sentinel, leave_mode="new")

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

    async def _noop(**_kwargs: object) -> None:
        pass

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...
        def show_stop_ack(self, _text: str) -> None: ...

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_Driver(), coord=coord, solicit=_solicit, autonomous=True,
        leave_mode="new")
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
    monkeypatch.setattr("imp.cli.main._handle_terminal_outcome", lambda *_: 1)

    coord = InterruptCoordinator()
    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=coord, solicit=lambda _p: _never(),
        leave_mode="new")

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


def test_terminal_rendering_merged_from_archive_when_home_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    quest = _archive_reaped_quest(
        tmp_path, "2026-07-03-render", status="merged",
        events=_INTENT_EVENT_LINE + _MERGED_EVENT_LINE)
    quest.title = ""                          # untitled, so the identity line shows the intent
    rc = main._session_exit_code(quest, SimpleNamespace(interrupted=False))
    out = capsys.readouterr().out
    lines = out.split("\n")
    assert rc == 0
    assert lines[0] == "merged into main"     # merged=True from the archived quest_merged event
    assert "done · commits on" not in out     # not the unmerged headline
    # the opening intent, too, comes from the archived blob
    assert lines[1] == "2026-07-03-render · > resolve me from archive"


def test_terminal_rendering_pauses_when_home_reaped_and_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    quest = _make_quest(tmp_path, "2026-07-03-norecord")
    assert not quest.state_dir.exists()
    rc = main._session_exit_code(quest, SimpleNamespace(interrupted=False))
    out = capsys.readouterr().out
    assert rc == 1                            # an unresolvable status renders, never raises
    assert out.split("\n")[0] == "paused"
    assert "resume: imp resume 2026-07-03-norecord" in out


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

    async def _capture(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _capture)
    monkeypatch.setattr("imp.cli.main._session_exit_code", lambda *_: 0)

    driver = _NullDriver()
    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=driver, coord=coord, solicit=lambda _p: _never(),
        leave_mode="new")

    assert captured["local_echo"] == driver.seed_pending_reply
    assert rc == 0


@pytest.mark.anyio
async def test_run_session_client_installs_startup_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(tmp_path, "2026-06-15-startup")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step=None, step_attempt=0, status="merged").save(
        quest.state_dir)

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
        quest, driver=_Driver(), coord=coord, solicit=lambda _p: _never(),
        leave_mode="new")

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

    async def _noop(**_kwargs: object) -> None:
        pass

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)

    class _Driver:
        def on_event(self, _event: Any) -> None: ...
        def finish(self) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...
        def show_stop_ack(self, _text: str) -> None: ...

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_Driver(), coord=coord, solicit=_solicit,
        model="anthropic:claude-fable-5", leave_mode="new")

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

    async def _noop(**_kwargs: object) -> None:
        pass

    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)

    class _Driver:
        def on_event(self, _event: Any) -> None: ...
        def finish(self) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...
        def show_stop_ack(self, _text: str) -> None: ...

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_Driver(), coord=coord, solicit=_solicit,
        opening_unsandboxed=True, leave_mode="new")

    assert rc == 0
    assert captured["opening_unsandboxed"] is True


def test_run_session_client_leave_mode_is_required() -> None:
    import inspect
    p = inspect.signature(run_session_client).parameters["leave_mode"]
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
        quest, driver=_NullDriver(), coord=coord, solicit=lambda _p: _never(),
        leave_mode="new")

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

    async def _noop(**_kwargs: object) -> None:
        pass
    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)

    class _Driver:
        def __init__(self) -> None:
            self.acks: list[str] = []
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...
        def show_stop_ack(self, text: str) -> None:
            self.acks.append(text)

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    drv = _Driver()
    rc = await run_session_client(
        quest, driver=drv, coord=coord, solicit=_solicit,
        leave_mode="new", non_interactive=False)

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

    async def _noop(**_kwargs: object) -> None:
        pass
    monkeypatch.setattr("imp.cli.session_run.run_terminal_client", _noop)

    class _Driver:
        def on_event(self, _event: dict[str, Any]) -> None: ...
        def finish(self) -> None: ...
        def seed_pending_reply(self, _text: str) -> None: ...
        def install_startup_status(self) -> None: ...
        def show_stop_ack(self, _text: str) -> None:  # pragma: no cover
            raise AssertionError("headless run must not render an interrupt ack")

    async def _solicit(_prompt: str, _interrupt: bool = False) -> str:  # pragma: no cover
        return ""

    coord = InterruptCoordinator()
    rc = await run_session_client(
        quest, driver=_Driver(), coord=coord, solicit=_solicit,
        leave_mode="new", non_interactive=True)

    assert rc == 0
    assert coord._ack_hook is None                # never registered in a headless run


async def _faulted(**_kwargs: Any) -> StreamEnding:
    return StreamEnding.FAULT


@pytest.mark.anyio
async def test_stream_fault_with_merged_state_renders_the_completed_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    # The daemon finished in the milliseconds after the fault (the behi75 case).
    quest = _make_quest(tmp_path, "2026-07-04-fault-merged")
    State(id=quest.id, step=None, step_attempt=0, status="merged").save(
        quest.state_dir)
    quest.event_log().append(agent_id=None, type="quest_merged", payload={})
    monkeypatch.setattr(session_run, "run_terminal_client", _faulted)

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), leave_mode="new")
    out = capsys.readouterr().out
    assert rc == 0
    assert out.split("\n")[0] == "merged into main"
    assert "lost the connection" not in out


@pytest.mark.anyio
async def test_stream_fault_without_a_terminal_state_reports_lost_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _make_quest(tmp_path, "2026-07-04-fault-live")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step="plan", step_attempt=0, status="paused").save(
        quest.state_dir)
    monkeypatch.setattr(session_run, "run_terminal_client", _faulted)

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), leave_mode="new")
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
async def test_clean_ending_still_renders_the_paused_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.session_run as session_run

    monkeypatch.setenv("NO_COLOR", "1")
    quest = _make_quest(tmp_path, "2026-07-04-clean-leave")
    (project_state_dir(tmp_path) / "worktrees" / quest.id).mkdir(parents=True)
    State(id=quest.id, step="plan", step_attempt=0, status="paused").save(
        quest.state_dir)

    async def _clean(**_kwargs: Any) -> StreamEnding:
        return StreamEnding.CLEAN

    monkeypatch.setattr(session_run, "run_terminal_client", _clean)

    rc = await session_run.run_session_client(
        quest, driver=_NullDriver(), coord=InterruptCoordinator(),
        solicit=lambda _p: _never(), leave_mode="new")
    out = capsys.readouterr().out
    assert rc == 1
    assert out.split("\n")[0] == "paused"
    assert f"resume: imp resume {quest.id}" in out
    assert "lost the connection" not in out
