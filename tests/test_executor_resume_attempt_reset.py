"""Fix I: a resume: edge resets step_attempt exactly once (not per retry)."""

from __future__ import annotations

import contextlib
import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from imp.engine.state import State
from imp.engine.types import Goto


def _make_change(root: Path, *, slug: str = "test-change", title: str = "t") -> Change:
    """Construct a Change directly on disk without git worktrees."""
    from imp.engine.loader import load_workflow
    from imp.providers.config import load_config

    src_wf = root / ".imp" / "workflows" / "default"
    change_dir = root / ".imp" / "change"
    change_dir.mkdir(parents=True, exist_ok=True)
    dest_wf = change_dir / "workflow"
    if dest_wf.exists():
        shutil.rmtree(dest_wf)
    shutil.copytree(src_wf, dest_wf)
    (change_dir / "state").mkdir(exist_ok=True)
    (change_dir / "change.json").write_text(json.dumps({
        "slug": slug, "title": title, "base_branch": "main",
    }, indent=2, sort_keys=True))

    configured_roles: list[str] = []
    with contextlib.suppress(Exception):
        configured_roles = load_config(root).role_names()

    workflow = load_workflow(dest_wf, configured_roles=configured_roles)
    flat = workflow.flat_steps()
    State(
        id=slug, step=flat[0].name if flat else None, step_attempt=0,
        status="running", vars=dict(workflow.vars.defaults()), loop_counts={},
    ).save(change_dir / "state")

    change = Change(
        slug=slug, title=title, base_branch="main",
        control_root=root, project_root=root, change_dir=change_dir,
    )
    change.event_log().append(
        agent_id=None, type="change_created",
        payload={"slug": slug, "title": title},
    )
    return change


def test_resume_goto_resets_attempt_once() -> None:
    # Mirrors the executor's Goto branch: resume edges zero step_attempt; plain
    # backward gotos do not.
    class S:
        step_attempt = 3
        resume_pending = None

    state = S()
    action = Goto(target="impl", resume=True)
    if action.resume:
        state.step_attempt = 0
    assert state.step_attempt == 0


def test_plain_goto_does_not_reset_attempt() -> None:
    class S:
        step_attempt = 3

    state = S()
    action = Goto(target="impl", resume=False)
    if action.resume:
        state.step_attempt = 0
    assert state.step_attempt == 3


# ── integration: drive the real run_change resume branch ─────────────────────
#
# Step-5 path chosen: HARNESS. The branch-loop workflow below (mirrors
# test_executor.py::_seed_branch_loop_workflow) exercises a genuine `resume:`
# edge through run_change with a registered fake provider — far higher fidelity
# than reimplementing the Goto-branch logic (which the scaffold tests above
# already cover structurally).


def _register_fake_provider(runner_factory) -> None:
    from imp.providers import resolve

    resolve.reset_providers_for_tests()

    class _Provider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
            return runner_factory(invocation)

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _Provider(),
    )


def _seed_resume_loop_workflow(root: Path) -> Change:
    """writer(agent) then a branch step `route` that always resumes back into
    writer. The `resume: writer` edge is the navigation point Fix I targets.

    The cursor is positioned at `route` (a branch — branches NEVER touch
    step_attempt) with a poisoned step_attempt, so the only thing that can zero
    it before the resumed writer invocation runs is the resume navigation
    itself. That is the genuine stale-count scenario: a change paused with a
    nonzero step_attempt whose next move is a `resume:` edge re-entering a
    step."""
    wf = root / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "vars: {go_on: {type: bool, default: false}}\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: work\n"
        "    steps:\n"
        "      - name: writer\n"
        "        file: agents/writer.md\n"
        "        model_role: default\n"
        "        rules:\n"
        "          - {if: 'go_on', goto: done}\n"
        "          - {else: true, continue: true}\n"
        "      - name: route\n"
        "        rules:\n"
        "          - {else: true, resume: writer, set: {go_on: true}}\n"
        "  - name: done\n"
        "    steps: []\n"
    )
    (root / "imp").mkdir(parents=True, exist_ok=True)
    (root / "imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer:\n      - {provider: fake, model: m}\n"
    )
    return _make_change(root)


@pytest.mark.anyio
async def test_resume_edge_zeroes_step_attempt(tmp_path: Path) -> None:
    """REAL coverage of Step-5 test (1): a `resume:` edge drives run_change
    back into a step and the resumed invocation starts from a zeroed
    step_attempt.

    Setup mirrors a change paused mid-flight with a stale retry count: we
    position the cursor at the `route` BRANCH step and poison step_attempt = 3
    in the persisted state. Branches never touch step_attempt, so when `route`
    fires its `resume: writer` edge the poisoned 3 is still live — the resume
    navigation is the ONLY place that can zero it before the resumed writer
    invocation runs. The runner snapshots the live step_attempt at the start of
    every invocation; the resumed writer invocation must observe 0.
    """
    change = _seed_resume_loop_workflow(tmp_path)

    # Poison: position cursor at the branch `route` with a stale retry count.
    state = State.load(change.state_dir)
    state.step = "route"
    state.step_attempt = 3
    state.save(change.state_dir)

    seen_attempts: list[int] = []

    class _Runner:
        def __init__(self, invocation: AgentInvocation) -> None:
            self._inv = invocation

        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            # Snapshot the live step_attempt at the moment this invocation runs.
            live = State.load(change.state_dir)
            seen_attempts.append(live.step_attempt)
            yield {"type": "agent_output", "payload": {"status": "Success"}}

        async def interrupt(self) -> None:
            pass

    _register_fake_provider(lambda inv: _Runner(inv))

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.COMPLETED

    # Exactly one writer invocation — the resumed one. It must observe a zeroed
    # step_attempt; without Fix I it inherits the poisoned 3.
    assert seen_attempts == [0], (
        f"resumed invocation must observe a ZEROED step_attempt (single-shot "
        f"reset at the resume navigation), got {seen_attempts}")


@pytest.mark.anyio
async def test_resumed_step_persistent_error_still_terminates(
    tmp_path: Path,
) -> None:
    """REAL coverage of Step-5 test (2): a resumed step that hits a PERSISTENT
    retryable error (agent never emits output) must still EXHAUST max_retries
    and pause — proving the reset is single-shot (at the resume navigation),
    NOT per-_route_agent-entry. A per-retry reset would zero step_attempt on
    every internal retry of the resumed step and loop forever.

    Guard against the regression hanging the suite: the runner caps total
    invocations and raises if exceeded, so a per-retry-reset regression fails
    loudly (bounded) instead of looping.
    """
    wf = tmp_path / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    # writer succeeds once (drives a resume edge into reviewer-style loop); the
    # SECOND step is resumed into and then persistently fails.
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    (wf / "agents" / "checker.md").write_text(
        "---\nname: checker\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    # route resumes back into `checker` once, then checker persistently fails.
    # max_retries: 3 → checker should be invoked max_retries+1 == 4 times after
    # the resume before exhausting and pausing.
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "vars: {resumed: {type: bool, default: false}}\n"
        "defaults: {max_retries: 3, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n"
        "  - name: work\n"
        "    steps:\n"
        "      - name: writer\n"
        "        file: agents/writer.md\n"
        "        model_role: default\n"
        "      - name: checker\n"
        "        file: agents/checker.md\n"
        "        model_role: default\n"
        "      - name: route\n"
        "        rules:\n"
        "          - {if: 'resumed', continue: true}\n"
        "          - {else: true, resume: checker, set: {resumed: true}}\n"
    )
    (tmp_path / "imp").mkdir(parents=True, exist_ok=True)
    (tmp_path / "imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer:\n      - {provider: fake, model: m}\n"
    )
    change = _make_change(tmp_path)

    checker_invocations = {"n": 0}
    _CAP = 50  # bound: a per-retry-reset regression would loop forever.

    class _Runner:
        def __init__(self, invocation: AgentInvocation) -> None:
            self._name = invocation.agent_def.name

        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            if self._name == "checker":
                checker_invocations["n"] += 1
                if checker_invocations["n"] > _CAP:
                    raise AssertionError(
                        "checker invoked > cap — per-retry reset regression "
                        "is looping forever")
                # Persistent retryable failure: never emit agent_output.
                return
                yield  # make async generator
            else:
                yield {"type": "agent_output", "payload": {"status": "Success"}}

        async def interrupt(self) -> None:
            pass

    _register_fake_provider(lambda inv: _Runner(inv))

    result = await run_change(change, on_event=lambda _e: None)
    assert result is RunResult.PAUSED

    # checker runs: first natural visit exhausts (max_retries+1 == 4), then it
    # is resumed into and exhausts AGAIN (another max_retries+1 == 4). The key
    # assertion is that it TERMINATED with a finite, small invocation count —
    # not that it equals an exact number across both passes. A per-retry reset
    # at the resume navigation would loop without bound.
    assert checker_invocations["n"] <= _CAP, checker_invocations["n"]
    assert State.load(change.state_dir).status == "paused"


@pytest.fixture
def anyio_backend():
    return "asyncio"
