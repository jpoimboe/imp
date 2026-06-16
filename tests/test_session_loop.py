from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.cli.main import _decide_resume_action, _run_change_session
from imp.cli.signals import InterruptCoordinator
from imp.engine.change import Change
from imp.engine.state import State


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- Commit 1: the pure decision core -------------------------------------


def test_decide_resume_bare_enter_empty_queue_reprompts() -> None:
    assert _decide_resume_action(line="", queued=False, slash=None) == "reprompt"


def test_decide_resume_bare_enter_with_queue_resumes() -> None:
    assert _decide_resume_action(line="", queued=True, slash=None) == "resume"


def test_decide_resume_continue_resumes() -> None:
    assert _decide_resume_action(line="/continue", queued=False, slash="continue") == "resume"


def test_decide_resume_typed_line_resumes() -> None:
    assert _decide_resume_action(line="do more", queued=False, slash=None) == "resume"


# --- Commit 2: integration / lifecycle against the REAL helper ------------


def _make_change(root: Path, *, slug: str = "c", title: str = "t") -> Change:
    """Construct a Change directly on disk without git worktrees (mirrors
    tests/test_executor_interrupt_resume.py)."""
    from imp.engine.loader import load_workflow
    from imp.providers.effective import load_effective_config

    src_wf = root / "imp" / "workflows" / "imp"
    change_dir = root / ".imp" / "change"
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "state").mkdir(exist_ok=True)
    (change_dir / "change.json").write_text(json.dumps({
        "id": slug, "title": title, "base_branch": "main",
    }, indent=2, sort_keys=True))

    configured_tiers: list[str] = []
    with contextlib.suppress(Exception):
        configured_tiers = load_effective_config(
            project_root=root, control_root=root).config.tier_names()

    workflow = load_workflow(src_wf, configured_tiers=configured_tiers)
    flat = workflow.flat_steps()
    State(
        id=slug, step=flat[0].name if flat else None, step_attempt=0,
        status="running", vars=dict(workflow.vars.defaults()), loop_counts={},
    ).save(change_dir / "state")

    change = Change(
        id=slug, title=title, base_branch="main",
        control_root=root, project_root=root, change_dir=change_dir,
    )
    change.event_log().append(
        agent_id=None, type="change_created",
        payload={"id": slug, "slug": change.slug, "title": title},
    )
    return change


def _seed_single_agent(root: Path) -> Change:
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 3, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: medium}\n"
    )
    (root / "imp").mkdir(parents=True, exist_ok=True)
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(
        "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n"
    )
    return _make_change(root)


class _StubDriver:
    """Faithful stand-in for LiveDriver: an on_event sink and a finish()."""

    def __init__(self) -> None:
        self.finished = 0

    def on_event(self, _e: dict) -> None:
        pass

    def finish(self) -> None:
        self.finished += 1


class _StubStatus:
    """Faithful stand-in for LiveStatus: the methods the session loop / ack
    hook actually call. No TTY."""

    def __init__(self) -> None:
        self.resets = 0
        self.acks: list[str] = []

    def reset(self) -> None:
        self.resets += 1

    def show_stop_ack(self, text: str) -> None:
        self.acks.append(text)


class _StubReader:
    """A pre-armed ESC reader stand-in. The session loop only reads `_armed`
    on the latch; the respawn path is skipped because coord._esc_reader is set
    truthy (reader_exited=False)."""

    _armed = True


def _make_replies(lines: list[str]) -> Any:
    """A reply_input_fn that yields each queued line then raises EOFError (the
    loop's outer Ctrl-D arm)."""
    pending = list(lines)

    async def _reply() -> str:
        if pending:
            return pending.pop(0)
        raise EOFError

    return _reply


async def _drive_session(
    change: Change, coord: InterruptCoordinator,
    *, replies: list[str],
    on_tty: bool = True,
) -> tuple[int, _StubDriver, _StubStatus]:
    """Construct the minimal real objects and drive the REAL
    _run_change_session inside a real task group. Returns (rc, driver, status)."""
    driver = _StubDriver()
    status = _StubStatus()
    # Skip the respawn path: a truthy _esc_reader means reader_exited is False.
    coord._esc_reader = object()

    rc_holder: dict[str, int] = {}

    async def _runner() -> None:
        async with anyio.create_task_group() as tg:
            rc_holder["rc"] = await _run_change_session(
                change, coord=coord, driver=driver, status=status,
                esc_reader=_StubReader(),  # type: ignore[arg-type]
                reply_input_fn=_make_replies(replies),
                non_interactive=False, tg=tg)

    if on_tty:
        # Force the interactive-loop predicate true without a real /dev/tty.
        orig_in, orig_out = sys.stdin.isatty, sys.stdout.isatty
        sys.stdin.isatty = lambda: True   # type: ignore[method-assign]
        sys.stdout.isatty = lambda: True  # type: ignore[method-assign]
        try:
            await _runner()
        finally:
            sys.stdin.isatty = orig_in    # type: ignore[method-assign]
            sys.stdout.isatty = orig_out  # type: ignore[method-assign]
    else:
        await _runner()

    return rc_holder["rc"], driver, status


def _register_fake(factory: Any) -> None:
    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    resolve.register_provider("fake", env_check=lambda: True, factory=factory)


@pytest.mark.anyio
async def test_interrupt_then_typed_line_resumes(tmp_path: Path) -> None:
    """Round 1 interrupts mid-turn; a typed line re-prompts the run and round 2
    completes. Asserts: one workflow_paused (interrupt_reason) in round 1, one
    workflow_resumed before round 2, status flips paused→running on re-entry,
    and the typed line was enqueued (drained at the round-2 turn boundary)."""
    change = _seed_single_agent(tmp_path)
    coord = InterruptCoordinator()
    rounds = {"n": 0}
    drained: list[str | None] = []
    seen_status_on_reentry: list[str] = []

    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            rounds["n"] += 1
            if rounds["n"] == 1:
                # Mid-turn interrupt: flip should_pause, yield one event so the
                # per-event check returns paused (cooperative interrupt).
                await coord.signal_pause(reason="esc")
                yield {"type": "agent_output", "payload": {"status": "Success"}}
            else:
                # Re-entry: observe the persisted running status + the interject
                # that the prompt enqueued, then complete.
                seen_status_on_reentry.append(
                    State.load(change.state_dir).status)
                drained.append(coord.drain_interject())
                yield {"type": "agent_output", "payload": {"status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()

    _register_fake(lambda _m: _P())

    rc, _driver, _status = await _drive_session(
        change, coord, replies=["do more"])

    assert rc == 0  # round 2 completed
    events = list(change.event_log().read())
    paused = [e for e in events if e.get("type") == "workflow_paused"]
    resumed = [e for e in events if e.get("type") == "workflow_resumed"]
    assert len(paused) == 1
    # The executor's interrupt pause carries the originating gesture as
    # interrupt_reason (E3); reason is the generic "interrupt" marker.
    payload = paused[0].get("payload") or {}
    assert payload.get("interrupt_reason") == "esc"
    assert len(resumed) == 1
    # The session loop set status=running on re-entry, observed inside round 2.
    assert seen_status_on_reentry == ["running"]
    # The typed line was enqueued and drained at the round-2 turn boundary.
    assert drained == ["do more"]
    assert rounds["n"] == 2


@pytest.mark.anyio
async def test_ctrl_d_at_resume_prompt_pauses_and_exits(tmp_path: Path) -> None:
    """Round 1 interrupts; the user presses Ctrl-D at the resume prompt (the
    reply_input_fn raises EOFError). Per the plan, that must pause-and-exit with
    rc 130 (reason ctrl-d), NOT crash with an uncaught EOFError."""
    change = _seed_single_agent(tmp_path)
    coord = InterruptCoordinator()

    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            await coord.signal_pause(reason="esc")
            yield {"type": "agent_output", "payload": {"status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()

    _register_fake(lambda _m: _P())

    # No replies → the resume prompt immediately raises EOFError (Ctrl-D).
    rc, _driver, _status = await _drive_session(change, coord, replies=[])

    assert rc == 130
    assert State.load(change.state_dir).status == "paused"
    paused = [e for e in change.event_log().read()
              if e.get("type") == "workflow_paused"]
    # The executor emitted workflow_paused(esc) in round 1; the ctrl-d
    # finalize is suppressed by the journal-tail guard (open pause already
    # exists) → exactly one workflow_paused in the log.
    assert len(paused) == 1
    assert paused[0]["payload"]["interrupt_reason"] == "esc"


@pytest.mark.anyio
async def test_two_interrupt_rounds_do_not_reuse_run_scope(tmp_path: Path) -> None:
    """Review B1: interrupt→prompt→resume TWICE. A reused single-use CancelScope
    would raise RuntimeError on round 2; assert a clean terminal outcome with no
    RuntimeError (a fresh scope per round)."""
    change = _seed_single_agent(tmp_path)
    coord = InterruptCoordinator()
    rounds = {"n": 0}

    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            rounds["n"] += 1
            # Rounds 1 and 2 both interrupt; round 3 completes.
            if rounds["n"] <= 2:
                await coord.signal_pause(reason="ctrl-c")
                yield {"type": "agent_output", "payload": {"status": "Success"}}
            else:
                yield {"type": "agent_output", "payload": {"status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()

    _register_fake(lambda _m: _P())

    rc, _driver, _status = await _drive_session(
        change, coord, replies=["more", "more again"])

    assert rc == 0
    assert rounds["n"] == 3  # two interrupt rounds + one completing round
    events = list(change.event_log().read())
    assert sum(1 for e in events if e.get("type") == "workflow_paused") == 2
    assert sum(1 for e in events if e.get("type") == "workflow_resumed") == 2


@pytest.mark.anyio
async def test_merge_completed_run_does_not_reload_removed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A merge-completing run finalizes inside _complete by removing the worktree
    (branch kept) — the change state dir, and its state.json, go with it. The
    session loop must surface the COMPLETED outcome (rc 0) WITHOUT reloading State
    from the now-absent dir.

    Regression: _run_change_session eagerly called State.load(change.state_dir)
    after EVERY round, before deciding loopability. On the merge path the state
    dir was already gone, so the load raised FileNotFoundError and the run crashed
    with an unhandled TaskGroup exception immediately after `change_merged`."""
    import shutil

    import imp.cli.main as main_mod
    from imp.engine.executor import RunResult

    change = _seed_single_agent(tmp_path)
    coord = InterruptCoordinator()

    async def _fake_run_change(ch: Change, **_kw: Any) -> RunResult:
        # Mirror _complete's merge branch: by the time the run returns COMPLETED,
        # remove_worktree has taken the worktree — state dir included — with it.
        shutil.rmtree(ch.state_dir)
        return RunResult.COMPLETED

    monkeypatch.setattr(main_mod, "run_change", _fake_run_change)

    rc, driver, _status = await _drive_session(change, coord, replies=[])

    assert rc == 0
    assert driver.finished >= 1
    # The terminal handler treats an absent worktree as "merged".
    assert not change.state_dir.exists()


@pytest.mark.anyio
async def test_sigterm_mid_run_exits_paused_no_unbound_result(tmp_path: Path) -> None:
    """Review B2: SIGTERM mid-run cancels the run scope → `result` is unbound that
    round; the loop must exit via the exit_event branch (status=paused, rc 130),
    never UnboundLocalError. Fire exit_event from inside the run."""
    change = _seed_single_agent(tmp_path)
    coord = InterruptCoordinator()

    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            # Simulate SIGTERM mid-run: set pause state + fire the exit event the
            # actuator watches, then block forever (the actuator cancels the run
            # scope, so run_change never returns a value this round).
            coord.pause_reason = "sigterm"
            coord._should_pause = True
            fn = coord._trigger_exit
            assert fn is not None  # the session loop wired the exit hook
            fn("sigterm")
            await anyio.sleep_forever()
            yield {"type": "agent_output", "payload": {"status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()

    _register_fake(lambda _m: _P())

    rc, driver, _status = await _drive_session(change, coord, replies=[])

    # B2: the round's `result` was never bound (the run scope was cancelled), yet
    # the loop exits cleanly via the exit_event branch — rc 130, status paused,
    # NO UnboundLocalError. pause_and_exit persisted the running→paused flip.
    assert rc == 130
    assert State.load(change.state_dir).status == "paused"
    assert driver.finished >= 1


@pytest.mark.anyio
@pytest.mark.parametrize("reason", ["sigterm", "ctrl-c"])
async def test_exit_during_park_writes_single_workflow_paused(
    tmp_path: Path, reason: str,
) -> None:
    """Point A (clean-solicit SIGINT) and SIGTERM-mid-run: the run is PARKED (no
    event yielded), so the executor emits no workflow_paused itself. The session
    loop's exit-event finalize must write EXACTLY one workflow_paused (none
    dropped) and leave state paused."""
    change = _seed_single_agent(tmp_path)
    coord = InterruptCoordinator()
    blocked = anyio.Event()  # never set

    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            # Request the out-of-band exit, then block until cancelled — no event
            # crosses the executor's per-event should_pause boundary, so the only
            # workflow_paused is the one the finalize appends.
            coord.request_exit(reason)
            await blocked.wait()
            yield {"type": "agent_output", "payload": {"status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()

    _register_fake(lambda _m: _P())

    rc, _driver, _status = await _drive_session(change, coord, replies=[])

    assert rc == 130
    assert State.load(change.state_dir).status == "paused"
    paused = [e for e in change.event_log().read()
              if e.get("type") == "workflow_paused"]
    assert len(paused) == 1
    assert paused[0]["payload"]["reason"] == reason


@pytest.mark.anyio
@pytest.mark.parametrize("pause_reason", [None, "sigterm"])
async def test_keyboardinterrupt_at_resume_prompt_pauses_cleanly(
    tmp_path: Path, pause_reason: str | None,
) -> None:
    """Round 1 interrupts to the loopable resume prompt; a second Ctrl-C (or a
    SIGTERM) makes the reply raise KeyboardInterrupt. The session must pause-exit
    cleanly (rc 130, no KeyboardInterrupt / BaseExceptionGroup escaping), stay
    paused, and surface the right reason."""
    change = _seed_single_agent(tmp_path)
    coord = InterruptCoordinator()

    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            await coord.signal_pause(reason="esc")
            yield {"type": "agent_output", "payload": {"status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()

    _register_fake(lambda _m: _P())

    driver = _StubDriver()
    status = _StubStatus()
    coord._esc_reader = object()

    async def _reply() -> str:
        # The session loop calls coord.reset() before the resume prompt, so
        # coord.pause_reason is None here. A SIGTERM at the prompt sets pause_reason
        # ("sigterm") before the KeyboardInterrupt unwinds out of _ask_or_exit (D1
        # decision); a bare second Ctrl-C leaves it None → the arm falls back to
        # "ctrl-c". Both must finalize cleanly.
        if pause_reason is not None:
            coord.pause_reason = pause_reason
        raise KeyboardInterrupt

    rc_holder: dict[str, int] = {}

    async def _runner() -> None:
        async with anyio.create_task_group() as tg:
            rc_holder["rc"] = await _run_change_session(
                change, coord=coord, driver=driver, status=status,
                esc_reader=_StubReader(),  # type: ignore[arg-type]
                reply_input_fn=_reply,
                non_interactive=False, tg=tg)

    import sys
    orig_in, orig_out = sys.stdin.isatty, sys.stdout.isatty
    sys.stdin.isatty = lambda: True   # type: ignore[method-assign]
    sys.stdout.isatty = lambda: True  # type: ignore[method-assign]
    try:
        await _runner()
    finally:
        sys.stdin.isatty = orig_in    # type: ignore[method-assign]
        sys.stdout.isatty = orig_out  # type: ignore[method-assign]

    assert rc_holder["rc"] == 130
    assert State.load(change.state_dir).status == "paused"
    paused = [e for e in change.event_log().read()
              if e.get("type") == "workflow_paused"]
    # The executor emitted workflow_paused(esc) in round 1; the KI finalize
    # is suppressed by the journal-tail guard (open pause already exists) →
    # exactly one workflow_paused in the log.
    assert len(paused) == 1
    assert paused[0]["payload"]["interrupt_reason"] == "esc"


@pytest.mark.anyio
async def test_executor_emitted_then_exit_event_writes_single_workflow_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGTERM mid-ACTIVE-run: _dispatch_signal sets should_pause SYNCHRONOUSLY, so
    the executor can reach a should_pause boundary, emit workflow_paused, and return
    RunResult.PAUSED BEFORE the actuator's async scope-cancel lands. exit_event is
    also set, so the loop takes the exit_event arm with result=PAUSED — and the
    finalize must NOT duplicate the executor's event. pause_and_exit's journal-tail
    guard reverse-scans the journal, finds the executor's workflow_paused before
    any workflow_resumed, and skips the append → exactly one workflow_paused."""
    import imp.cli.main as cli_main
    from imp.engine.executor import RunResult

    change = _seed_single_agent(tmp_path)
    coord = InterruptCoordinator()

    async def _emit_then_pause(
        change: Change, *, on_event: Any, user_chat_input_fn: Any,
        coordinator: Any, non_interactive: bool, seed_opening: Any = None,
    ) -> RunResult:
        # Reproduce the race deterministically: set pause state + fire the exit hook
        # the actuator watches, append the executor's own workflow_paused, then
        # RETURN PAUSED with NO await — so no checkpoint lets the actuator cancel
        # the run scope before run_change returns its value.
        coordinator.pause_reason = "sigterm"
        coordinator._should_pause = True
        assert coordinator._trigger_exit is not None  # session loop wired the hook
        coordinator._trigger_exit("sigterm")
        change.event_log().append(
            agent_id=None, type="workflow_paused",
            payload={"reason": "sigterm", "interrupt_reason": "sigterm"})
        return RunResult.PAUSED

    monkeypatch.setattr(cli_main, "run_change", _emit_then_pause)

    rc, _driver, _status = await _drive_session(change, coord, replies=[])

    assert rc == 130
    assert State.load(change.state_dir).status == "paused"
    paused = [e for e in change.event_log().read()
              if e.get("type") == "workflow_paused"]
    assert len(paused) == 1  # the executor's event, NOT duplicated by the finalize
    assert paused[0]["payload"]["reason"] == "sigterm"
