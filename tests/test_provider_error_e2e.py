from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from imp.engine.state import State


def _make_change(root: Path, *, quest_id: str = "test-change", title: str = "t") -> Change:
    """Construct a Change directly on disk without git worktrees."""
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

    change = Change(
        id=quest_id, title=title, base_branch="main",
        control_root=root, project_root=root, quest_dir=quest_dir,
    )
    change.event_log().append(
        agent_id=None, type="change_created",
        payload={"id": quest_id, "title": title},
    )
    return change


class _FatalErrorRunner:
    """Mimics a real provider that classified a genuine fatal (terminated) error."""

    def __init__(self) -> None:
        self.run_count = 0

    async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
        self.run_count += 1
        yield {"type": "error", "payload": {
            "category": "terminated",
            "kind": "TerminatedError",
            "message": "agent terminated with an error (cause unknown)",
            "detail": {"request_id": "req_01abc"},
        }}

    async def interrupt(self) -> None:
        pass


def _seed_workflow_with_max_retries_3(root: Path) -> Change:
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    # New inline-surface format: phases is a list of {name, steps}.
    # on_max_retries uses verb-style: {pause: true}.
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 3, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    _cfg = (
        "providers: [fake]\n"
        "tiers:\n"
        "  low: {fake: m}\n"
        "  medium: {fake: m}\n"
        "  high: {fake: m}\n"
    )
    # run_change reads via load_effective_config: project marker + local layer.
    (root / "imp").mkdir(parents=True, exist_ok=True)
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(_cfg)
    return _make_change(root)


@pytest.mark.anyio
async def test_fatal_failure_pauses_immediately_with_diagnostic(
    tmp_path: Path,
) -> None:
    """A genuine fatal (terminated) classified error produces a single error
    event, immediately Pauses (does NOT consume max_retries=3), and the event
    payload carries the diagnostic detail (category, message, request_id)."""
    change = _seed_workflow_with_max_retries_3(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    runner = _FatalErrorRunner()

    class _FatalProvider:
        name = "fake"
        model = "m"

        def create_runner(self, _inv: AgentInvocation) -> AgentRunner:
            return runner

    resolve.register_provider(
        "fake", env_check=lambda: True, factory=lambda _m: _FatalProvider(),
    )

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)

    assert result is RunResult.PAUSED

    assert runner.run_count == 1, (
        f"fatal failure should not consume retries; "
        f"ran {runner.run_count} times"
    )

    error_events = [e for e in events if e["type"] == "error"]
    assert len(error_events) == 1
    payload = error_events[0]["payload"]
    assert payload["category"] == "terminated"
    assert "terminated" in payload["message"]
    assert payload["detail"].get("request_id") == "req_01abc"

    state = State.load(change.state_dir)
    assert state.status == "paused"
    assert state.step_attempt == 0


@pytest.fixture
def anyio_backend():
    return "asyncio"
