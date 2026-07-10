"""Fix A: workflow_paused records the originating category/kind."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.engine.quest import Quest
from imp.engine.executor import RunResult, run_change
from imp.engine.state import State
from imp.engine.types import Continue, Goto, Pause


def _make_change(root: Path, *, quest_id: str = "test-change", title: str = "t") -> Quest:
    """Construct a Quest directly on disk without git worktrees."""
    from imp.engine.loader import load_workflow
    from imp.providers.effective import load_effective_config

    src_wf = root / "imp" / "workflows" / "imp"
    quest_dir = root / ".imp" / "change"
    quest_dir.mkdir(parents=True, exist_ok=True)
    (quest_dir / "state").mkdir(exist_ok=True)
    (quest_dir / "quest.json").write_text(json.dumps({
        "id": quest_id, "title": title, "base_branch": "main",
    }, indent=2, sort_keys=True))

    configured_tiers: list[str] = []
    with contextlib.suppress(Exception):
        configured_tiers = load_effective_config(
            project_root=root, control_root=root).config.tier_names()

    workflow = load_workflow(src_wf, configured_tiers=configured_tiers)
    flat = workflow.flat_steps()
    State(
        id=quest_id, step=flat[0].name if flat else None, step_attempt=0,
        status="running", vars=dict(workflow.vars.defaults()), loop_counts={},
    ).save(quest_dir / "state")

    change = Quest(
        id=quest_id, title=title, base_branch="main",
        control_root=root, project_root=root, quest_dir=quest_dir,
    )
    change.event_log().append(
        agent_id=None, type="change_created",
        payload={"id": quest_id, "title": title},
    )
    return change


def test_pause_has_reason_and_kind_fields() -> None:
    p = Pause(reason="api", kind="http_400")
    assert p.reason == "api"
    assert p.kind == "http_400"


def test_pause_defaults_are_none() -> None:
    p = Pause()
    assert p.reason is None
    assert p.kind is None


def test_emit_branch_for_non_pause_action_is_interrupt() -> None:
    # Mirrors the executor's emit-site guard: a coordinator interrupt can leave
    # `action` as Continue/Goto with no .reason. The branch must not AttributeError.
    for action in (Continue(), Goto(target="x")):
        reason, kind = (
            (action.reason, action.kind)  # type: ignore[attr-defined]
            if isinstance(action, Pause) else ("interrupt", None)
        )
        assert reason == "interrupt"
        assert kind is None


# ── end-to-end: workflow_paused payload carries the originating reason/kind ──
#
# Step-10 path chosen: HARNESS (not unit-stub). test_provider_error_e2e.py
# already wires a fake provider that yields a classified `error` event and runs
# it through run_change, so an end-to-end assertion on the emitted
# workflow_paused payload is both achievable and higher-fidelity than stubbing
# _run_agent_invocation. The fixture yields category="auth"/kind="AuthError"
# (a fatal Pause category) rather than the plan's illustrative "api"/"http_400";
# the assertions follow the real fixture's values.


def _seed_single_agent(root: Path) -> Quest:
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
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    # run_change reads via load_effective_config: project marker + local layer.
    (root / "imp").mkdir(parents=True, exist_ok=True)
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(
        "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n"
    )
    return _make_change(root)


@pytest.mark.anyio
async def test_fatal_error_records_reason_and_kind_on_workflow_paused(
    tmp_path: Path,
) -> None:
    """A category=terminated (fatal) error pauses immediately and the emitted
    workflow_paused payload carries reason=terminated / kind=TerminatedError,
    plumbed from the classified ErrorInfo through Pause(reason, kind)."""
    change = _seed_single_agent(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    class _FatalRunner:
        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            yield {"type": "error", "payload": {
                "category": "terminated",
                "kind": "TerminatedError",
                "message": "agent terminated with an error (cause unknown)",
            }}

        async def interrupt(self) -> None:
            pass

    class _FatalProvider:
        name = "fake"
        model = "m"

        def create_runner(self, _inv: AgentInvocation) -> AgentRunner:
            return _FatalRunner()

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _FatalProvider(),
    )

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)

    assert result is RunResult.PAUSED
    paused = [e for e in events if e["type"] == "workflow_paused"]
    assert len(paused) == 1
    assert paused[0]["payload"]["reason"] == "terminated"
    assert paused[0]["payload"]["kind"] == "TerminatedError"

    state = State.load(change.state_dir)
    assert state.status == "paused"


@pytest.mark.anyio
async def test_max_loops_pause_reason_unchanged(tmp_path: Path) -> None:
    """The max_loops pause path is unchanged by Fix A: its workflow_paused still
    emits reason=max_loops (it never routes through the agent fatal branch)."""
    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "  verdict: {type: enum, values: [approve, rework]}\n---\ngo\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 10}\n"
        "phases:\n"
        "  - name: work\n"
        "    steps:\n"
        "      - name: reviewer\n"
        "        file: agents/reviewer.md\n"
        "        tier: low\n"
        "        max_loops: 2\n"
        "        rules:\n"
        "          - {if: {verdict: rework}, goto: reviewer}\n"
        "          - {else: true, continue: true}\n"
    )
    # run_change reads via load_effective_config: project marker + local layer.
    (tmp_path / "imp").mkdir(parents=True, exist_ok=True)
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (tmp_path / ".imp").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".imp" / "config.yaml").write_text(
        "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n"
    )
    change = _make_change(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    class _ReworkRunner:
        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            yield {"type": "agent_output",
                   "payload": {"outcome": "ok", "status": "Success", "verdict": "rework"}}

        async def interrupt(self) -> None:
            pass

    class _Provider:
        name = "fake"
        model = "m"

        def create_runner(self, _inv: AgentInvocation) -> AgentRunner:
            return _ReworkRunner()

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _Provider(),
    )

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)

    assert result is RunResult.PAUSED
    paused = [e for e in events if e["type"] == "workflow_paused"]
    assert len(paused) == 1
    assert paused[0]["payload"]["reason"] == "max_loops"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_ctrl_d_interrupt_records_interrupt_reason_on_workflow_paused(
    tmp_path: Path,
) -> None:
    change = _seed_single_agent(tmp_path)
    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    from imp.cli.signals import InterruptCoordinator
    coord = InterruptCoordinator()

    class _InterruptingRunner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            await coord.signal_pause(reason="ctrl-d")
            yield {"type": "agent_output",
                   "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"

        def create_runner(self, _inv: AgentInvocation) -> AgentRunner:
            return _InterruptingRunner()

    resolve.register_provider("fake", env_check=lambda: True, factory=lambda _m: _P())

    events: list[dict] = []
    result = await run_change(change, on_event=events.append, coordinator=coord)
    assert result is RunResult.PAUSED
    paused = [e for e in events if e["type"] == "workflow_paused"]
    assert len(paused) == 1
    assert paused[0]["payload"]["interrupt_reason"] == "ctrl-d"


@pytest.mark.anyio
async def test_awaiting_user_park_records_awaiting_user_reason(
    tmp_path: Path,
) -> None:
    """A headless bare-turn-end yield (AwaitingUserInput with interrupt=False)
    parks with workflow_paused reason="awaiting_user" and NO interrupt_reason —
    the split away from the overloaded "interrupt" token."""
    change = _seed_single_agent(tmp_path)

    from imp.engine.awaiting_user_input import AwaitingUserInput
    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    class _AwaitingRunner:
        async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
            raise AwaitingUserInput("the agent's last question", interrupt=False)
            yield  # unreachable — makes run() an async generator

        async def interrupt(self) -> None:
            pass

    class _Provider:
        name = "fake"
        model = "m"

        def create_runner(self, _inv: AgentInvocation) -> AgentRunner:
            return _AwaitingRunner()

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _Provider(),
    )

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)

    assert result is RunResult.PAUSED
    paused = [e for e in events if e["type"] == "workflow_paused"]
    assert len(paused) == 1
    assert paused[0]["payload"]["reason"] == "awaiting_user"
    assert "interrupt_reason" not in paused[0]["payload"]

    # The step is marked resumable exactly as before (unchanged by the relabel).
    assert State.load(change.state_dir).resume_pending == "w"
