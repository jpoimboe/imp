# tests/test_headless_interrupt.py
"""Headless-guard tests: SIGINT must never park a piped/non-interactive run at
a prompt.

Three predicate-driven unit tests (a/b/c) each exercise
_interactive_loop_predicate at the exact arg combos that gate the session loop.
They are the primary hermetic guard: if any combination accidentally returns
True, the subprocess test below would hang and CI would break.

One integration test exercises _run_change_session directly (no subprocess
needed) with a headless setup and a mid-turn interrupt signal, asserting the
session exits paused (rc 130) without ever reaching the prompt-collection code.

Documented limitation (no pty test needed):
    kill -INT sent to a process whose stdin AND stdout are real ttys — i.e. a
    fully attended interactive session already mid-run — will park the process
    at the `> ` re-prompt if the ESC reader armed successfully.  This is the
    INTENDED behaviour (§2 of the interrupt-to-prompt spec): the user can type
    a follow-up or Ctrl-D to exit.  The guard below specifically covers the
    headless case (no /dev/tty, piped stdin, or --non-interactive) where
    parking would hang CI.  The tty-attended limitation is not tested here
    because driving it would require a real pty harness (see the impty harness
    in tests/impty/); the spec documents it as known behaviour, not a bug.
"""
from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.cli.main import _interactive_loop_predicate, _run_change_session
from imp.cli.signals import InterruptCoordinator
from imp.engine.change import Change
from imp.engine.state import State

# ---------------------------------------------------------------------------
# Step 1: Predicate-driven unit tests (a / b / c)
# ---------------------------------------------------------------------------


def test_flagless_fully_piped_never_loops() -> None:
    """(a) Flagless run with no /dev/tty: reader never arms, stdin+stdout piped.
    The interactive-loop predicate must return False so a SIGINT mid-run
    exits paused instead of parking at a prompt."""
    assert _interactive_loop_predicate(
        non_interactive=False, reader_armed=False,
        stdin_tty=False, stdout_tty=False) is False


def test_piped_stdin_with_tty_never_loops() -> None:
    """(b) `imp new < file`: reader armed via /dev/tty, but stdin is a pipe.
    Even with a tty output, a piped stdin must not park the run at a prompt
    consuming the pipe contents as conversation replies."""
    assert _interactive_loop_predicate(
        non_interactive=False, reader_armed=True,
        stdin_tty=False, stdout_tty=True) is False


def test_non_interactive_never_loops() -> None:
    """(c) --non-interactive: unconditionally non-looping regardless of tty
    state — an explicit user request to run headless."""
    assert _interactive_loop_predicate(
        non_interactive=True, reader_armed=False,
        stdin_tty=True, stdout_tty=True) is False


# ---------------------------------------------------------------------------
# Step 3: Integration characterization — headless wiring end-to-end
# ---------------------------------------------------------------------------
#
# Tests below exercise _run_change_session directly with a stub provider that
# blocks mid-turn until an interrupt signal is delivered in-process.  No real
# subprocess is spawned because:
#
#   • A subprocess would need a fake provider injectable via env (none exists),
#     or a real provider key (not hermetic), or a workflow that reliably blocks
#     for long enough to inject SIGINT and short enough not to flake.
#   • _run_change_session is the session loop itself — the exact code path that
#     reads _interactive_loop_predicate and decides whether to park at a prompt.
#   • Testing it at this level is HIGHER fidelity than a subprocess test: we
#     can observe coordinator state, driver.finish() counts, and the final rc
#     without process-start latency or editable-install races.
#
# Pattern mirrors test_session_loop.py's _drive_session helper.


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _StubDriver:
    """Faithful stand-in for LiveDriver used by _run_change_session."""

    def __init__(self) -> None:
        self.finished = 0

    def on_event(self, _e: dict) -> None:
        pass

    def finish(self) -> None:
        self.finished += 1


class _StubStatus:
    """Faithful stand-in for LiveStatus used by _run_change_session."""

    def __init__(self) -> None:
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def show_stop_ack(self, _text: str) -> None:
        pass


def _make_change(root: Path, *, slug: str = "c", title: str = "t") -> Change:
    """Construct a Change directly on disk without git worktrees (mirrors
    tests/test_session_loop.py)."""
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


def _seed_blocking_agent(root: Path) -> Change:
    """Seed a workflow with a single agent that blocks until interrupted."""
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "blocker.md").write_text(
        "---\nname: blocker\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\nblock\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 3, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - {name: s, file: agents/blocker.md, tier: medium}\n"
    )
    (root / "imp").mkdir(parents=True, exist_ok=True)
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(
        "providers: [fake]\n"
        "tiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n"
    )
    return _make_change(root)


def _never_reply() -> Any:
    """A reply_input_fn that should never be called (park = test failure)."""
    async def _reply() -> str:
        raise AssertionError(  # pragma: no cover
            "reply_input_fn was called — the headless guard failed: "
            "the session loop parked at a prompt on a non-interactive/piped run"
        )
    return _reply


async def _drive_headless(
    change: Change,
    coord: InterruptCoordinator,
    *,
    non_interactive: bool,
    reader_armed: bool,
) -> tuple[int, _StubDriver, _StubStatus]:
    """Drive _run_change_session in headless mode.  stdin/stdout isatty() are
    NOT patched so they reflect the actual (piped) pytest environment, which
    makes _interactive_loop_predicate return False without any extra mocking.

    For non_interactive=True, esc_reader=None and reader_armed=False.
    For non_interactive=False with reader_armed=False (fully-piped), we pass
    esc_reader=None as well (no reader ever started)."""
    driver = _StubDriver()
    status = _StubStatus()
    # No ESC reader: coord._esc_reader stays None so the respawn code-path
    # is never reached (it is not entered on non-loopable sessions anyway).

    rc_holder: dict[str, int] = {}

    async def _runner() -> None:
        async with anyio.create_task_group() as tg:
            rc_holder["rc"] = await _run_change_session(
                change, coord=coord, driver=driver, status=status,
                esc_reader=None,
                reply_input_fn=_never_reply(),
                non_interactive=non_interactive,
                tg=tg,
            )

    await _runner()
    return rc_holder["rc"], driver, status


def _register_interrupt_runner(coord: InterruptCoordinator) -> None:
    """Register a fake provider whose runner fires a signal_pause then yields
    one event so the executor's per-event should_pause check fires cleanly."""
    from imp.providers import resolve

    resolve.reset_providers_for_tests()

    class _InterruptingRunner:
        async def run(self, _inv: AgentInvocation) -> AsyncIterator[Event]:
            # Signal the pause (mirrors InterruptCoordinator.signal_pause
            # called by the real SIGINT handler mid-turn).
            await coord.signal_pause(reason="ctrl-c")
            # Yield one event so drive_invocation's per-event check fires.
            yield {"type": "agent_output", "payload": {"status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"

        def create_runner(self, _inv: AgentInvocation) -> AgentRunner:
            return _InterruptingRunner()

    resolve.register_provider("fake", env_check=lambda: True, factory=lambda _m: _P())


@pytest.mark.anyio
async def test_flagless_piped_sigint_exits_paused_not_at_prompt(
    tmp_path: Path,
) -> None:
    """Flagless fully-piped run: SIGINT mid-turn → rc 130 (paused), never parks
    at a prompt.  reader_armed=False because there is no /dev/tty; stdin/stdout
    are pipes (the pytest runner is non-interactive).

    This is the end-to-end headless guard: if _interactive_loop_predicate
    returned True here, _run_change_session would call reply_input_fn which
    raises AssertionError, failing the test immediately."""
    change = _seed_blocking_agent(tmp_path)
    coord = InterruptCoordinator()
    _register_interrupt_runner(coord)

    rc, driver, status = await _drive_headless(
        change, coord, non_interactive=False, reader_armed=False)

    assert rc == 130, f"expected rc 130 (paused), got {rc}"
    assert driver.finished == 1, "driver.finish() must be called on the exit path"
    assert status.resets == 1, "status.reset() must be called in the finally block"
    final = State.load(change.state_dir)
    assert final.status == "paused", f"state must be paused, got {final.status!r}"


@pytest.mark.anyio
async def test_non_interactive_sigint_exits_paused_not_at_prompt(
    tmp_path: Path,
) -> None:
    """--non-interactive run: SIGINT mid-turn → rc 130 (paused), never parks.
    non_interactive=True means reader_armed is forced False inside
    _run_change_session regardless of any reader passed in."""
    change = _seed_blocking_agent(tmp_path)
    coord = InterruptCoordinator()
    _register_interrupt_runner(coord)

    rc, driver, status = await _drive_headless(
        change, coord, non_interactive=True, reader_armed=False)

    assert rc == 130, f"expected rc 130 (paused), got {rc}"
    assert driver.finished == 1
    assert status.resets == 1
    final = State.load(change.state_dir)
    assert final.status == "paused", f"state must be paused, got {final.status!r}"
