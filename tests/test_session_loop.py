from __future__ import annotations

import contextlib
import json
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

    src_wf = root / "imp" / "workflows" / "default"
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
    wf = root / "imp" / "workflows" / "default"
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
    (root / "imp" / "project.yaml").write_text("workflow: default\n")
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
        import sys
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
