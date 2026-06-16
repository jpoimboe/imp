"""Tests for the C4 escalation actuator pattern.

Part 1: scope-only-cancellation pattern — the actuator cancels ONLY the run
scope, NOT the task group / sibling tasks.

Part 2: end-to-end escalation integration test — real _run_change_session drives
a run, an escalation fires mid-run, the loop takes the force-quit branch → rc
130, status paused, workflow_paused reason="force-quit" journaled.
"""
from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.cli.signals import InterruptCoordinator


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# Part 1: scope-only-cancellation pattern test
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_escalation_cancels_run_scope_only() -> None:
    """The actuator cancels the run scope when escalation fires, but does NOT
    cancel the outer task group or sibling tasks. coord.escalated is set True.

    Design: a sibling task sets a flag when it completes normally (after the
    run scope is cancelled and the test notifies it via an event). If the
    run_scope cancel had spilled to the task group, the sibling would be
    cancelled and never reach the flag-setting code. A separate
    `cancelled_by_tg` flag tracks whether the sibling was cancelled (it will
    be, but ONLY by the final tg.cancel_scope.cancel(), NOT by run_scope)."""
    coord = InterruptCoordinator()
    coord.escalation_event = anyio.Event()
    run_scope_exited = anyio.Event()    # main body signals: run scope done
    sibling_reached_checkpoint = {"ok": False}
    sibling_cancel_count = {"n": 0}

    async def actuator(scope: anyio.CancelScope) -> None:
        await coord.escalation_event.wait()
        coord.escalated = True
        scope.cancel()

    async with anyio.create_task_group() as tg:
        run_scope = anyio.CancelScope()
        tg.start_soon(actuator, run_scope)

        async def sibling() -> None:
            # Wait until the run scope exits (main body unblocked after cancel).
            # If the run_scope cancel had also cancelled this task, we'd never
            # get here — the checkpoint would remain False.
            await run_scope_exited.wait()
            sibling_reached_checkpoint["ok"] = True
            # Now wait forever until the task group tears down.
            try:
                await anyio.sleep_forever()
            except anyio.get_cancelled_exc_class():
                sibling_cancel_count["n"] += 1
                raise

        tg.start_soon(sibling)

        with run_scope:
            coord.set_escalation_hook(coord.escalation_event.set)
            coord._should_pause = True
            coord._first_gesture_at = 0.0
            coord._now = lambda: 5.0  # >1 s later → escalation branch
            import signal as _sig
            coord._dispatch_signal(_sig.SIGINT)  # escalates → sets the event
            await anyio.sleep_forever()           # run body; cancelled by actuator
        # run_scope.cancel() caused the above to unblock. Signal sibling.
        run_scope_exited.set()
        # Give the sibling a chance to observe the event and set its flag.
        await anyio.sleep(0)
        # Now tear down the task group (cancels the sibling's sleep_forever).
        tg.cancel_scope.cancel()

    assert coord.escalated is True
    # The sibling reached the checkpoint → run_scope cancel did NOT affect it.
    assert sibling_reached_checkpoint["ok"] is True
    # The sibling was cancelled exactly once, only by the tg teardown, not by run_scope.
    assert sibling_cancel_count["n"] == 1


# ---------------------------------------------------------------------------
# Part 2: end-to-end escalation integration test
# (reuses helpers from test_session_loop.py; replicated here for independence)
# ---------------------------------------------------------------------------


def _make_change_esc(root: Path, *, slug: str = "c", title: str = "t") -> Any:
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

    from imp.engine.change import Change
    from imp.engine.state import State

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


def _seed_esc(root: Path) -> Any:
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
    (root / "imp" / "project.yaml").write_text("workflow: default\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(
        "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n"
    )
    return _make_change_esc(root)


def _register_fake_esc(factory: Any) -> None:
    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    resolve.register_provider("fake", env_check=lambda: True, factory=factory)


class _StubDriverEsc:
    def __init__(self) -> None:
        self.finished = 0

    def on_event(self, _e: dict) -> None:
        pass

    def finish(self) -> None:
        self.finished += 1


class _StubStatusEsc:
    def __init__(self) -> None:
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def show_stop_ack(self, text: str) -> None:
        pass


class _StubReaderEsc:
    _armed = True


def _make_replies_esc(lines: list[str]) -> Any:
    pending = list(lines)

    async def _reply() -> str:
        if pending:
            return pending.pop(0)
        raise EOFError

    return _reply


async def _drive_session_esc(
    change: Any, coord: InterruptCoordinator,
    *, replies: list[str],
) -> tuple[int, _StubDriverEsc, _StubStatusEsc]:
    from imp.cli.main import _run_change_session

    driver = _StubDriverEsc()
    status = _StubStatusEsc()
    coord._esc_reader = object()  # skip respawn path

    rc_holder: dict[str, int] = {}

    async def _runner() -> None:
        async with anyio.create_task_group() as tg:
            rc_holder["rc"] = await _run_change_session(
                change, coord=coord, driver=driver, status=status,
                esc_reader=_StubReaderEsc(),  # type: ignore[arg-type]
                reply_input_fn=_make_replies_esc(replies),
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

    return rc_holder["rc"], driver, status


@pytest.mark.anyio
async def test_escalation_mid_run_exits_force_quit_rc130(tmp_path: Path) -> None:
    """End-to-end: a real _run_change_session run fires an escalation mid-turn.

    The _run_actuator._on_esc coroutine sets coord.escalated=True and cancels
    the run scope; the loop takes the coord.escalated branch →
    _finalize_paused_exit(reason="force-quit") → rc 130, status paused, and a
    workflow_paused event with reason="force-quit" is journaled."""
    from imp.agents.runner import AgentInvocation, AgentRunner, Event
    from imp.engine.state import State

    change = _seed_esc(tmp_path)
    coord = InterruptCoordinator()

    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            # Simulate escalation mid-run: the session loop has already wired
            # coord.escalation_event and set_escalation_hook by the time the
            # runner starts. Fire the escalation event directly (the actuator's
            # _on_esc is waiting on it in a sibling task), then block — the
            # actuator will set coord.escalated=True and cancel the run scope,
            # making this run_change call never return a value.
            assert coord.escalation_event is not None, "escalation_event not set by session loop"
            coord.escalated_by_test = True  # marker so we can confirm this path ran
            # Set should_pause + escalation_event directly (bypassing the
            # _dispatch_signal path, which is a synchronous signal callback
            # that can't be called in an async context here).
            coord._should_pause = True
            if coord.pause_reason is None:
                coord.pause_reason = "ctrl-c"
            coord.escalation_event.set()   # triggers _run_actuator._on_esc
            await anyio.sleep_forever()    # run body; scope cancelled by actuator
            yield {"type": "agent_output", "payload": {"status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()

    _register_fake_esc(lambda _m: _P())

    rc, driver, _status = await _drive_session_esc(change, coord, replies=[])

    # The run scope was cancelled, coord.escalated was set, the loop took the
    # force-quit branch → rc 130, status paused, workflow_paused journaled.
    assert rc == 130, f"expected rc=130 (force-quit), got {rc}"
    assert State.load(change.state_dir).status == "paused"
    assert driver.finished >= 1

    events = list(change.event_log().read())
    force_quit_paused = [
        e for e in events
        if e.get("type") == "workflow_paused"
        and (e.get("payload") or {}).get("reason") == "force-quit"
    ]
    assert force_quit_paused, (
        f"expected a workflow_paused(reason='force-quit') event; got events: {events}"
    )

    # Confirm the runner actually ran and set the marker.
    assert getattr(coord, "escalated_by_test", False), "runner never fired escalation"
