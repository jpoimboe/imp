"""Part 2: the transient-error retry sets/persists retry_continue_pending, feeds
it into resume_should_continue at the consume site, clears it on forward
progress, and sets its disposition from the pause reason."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from imp.engine.state import State


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_WRITER = (
    "---\nname: writer\ntools: [complete_step]\n"
    "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
)
_SINGLE_WF = (
    "name: t\ndescription: t\n"
    "defaults: {max_retries: 1, max_loops: 6, on_max_retries: {pause: true}}\n"
    "phases:\n"
    "  - name: only\n"
    "    steps:\n"
    "      - {name: w, file: agents/writer.md, tier: low}\n"
)


def _make_change(root: Path, *, agents: dict[str, str], workflow_yaml: str) -> Change:
    from imp.engine.loader import load_workflow
    from imp.providers.effective import load_effective_config

    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    for fname, body in agents.items():
        (wf / "agents" / fname).write_text(body)
    (wf / "workflow.yaml").write_text(workflow_yaml)

    (root / "imp").mkdir(parents=True, exist_ok=True)
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(
        "providers: [fake]\ntiers:\n"
        "  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n"
    )

    change_dir = root / ".imp" / "change"
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "state").mkdir(exist_ok=True)
    (change_dir / "change.json").write_text(json.dumps(
        {"id": "c", "title": "t", "base_branch": "main"}, indent=2, sort_keys=True))

    configured_tiers: list[str] = []
    with contextlib.suppress(Exception):
        configured_tiers = load_effective_config(
            project_root=root, control_root=root).config.tier_names()

    workflow = load_workflow(wf, configured_tiers=configured_tiers)
    flat = workflow.flat_steps()
    State(
        id="c", step=flat[0].name if flat else None, step_attempt=0,
        status="running", vars=dict(workflow.vars.defaults()), loop_counts={},
    ).save(change_dir / "state")

    change = Change(
        id="c", title="t", base_branch="main",
        control_root=root, project_root=root, change_dir=change_dir,
    )
    change.event_log().append(
        agent_id=None, type="change_created", payload={"id": "c", "title": "t"})
    return change


class _StallNthRunner:
    """Emits a 'stalled' error (→ RetryWithBackoff) when `stalled`, else succeeds."""

    def __init__(self, stalled: bool) -> None:
        self._stalled = stalled

    async def run(self, invocation: Any) -> Any:
        if self._stalled:
            yield {"type": "error", "payload": {
                "category": "stalled", "kind": "stalled",
                "message": "no events for 240s"}}
        else:
            yield {"type": "agent_output",
                   "payload": {"status": "Success", "outcome": "done"}}

    async def interrupt(self) -> None:
        pass


class _StallThenSucceedProvider:
    name = "fake"
    model = "m"

    def __init__(self, stalls: int) -> None:
        self.stalls = stalls
        self.drives = 0

    def create_runner(self, _inv: Any) -> _StallNthRunner:
        self.drives += 1
        return _StallNthRunner(self.drives <= self.stalls)


@pytest.mark.anyio
async def test_backoff_persists_retry_continue_pending_before_sleep(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(
        tmp_path, agents={"writer.md": _WRITER}, workflow_yaml=_SINGLE_WF)
    provider = _StallThenSucceedProvider(stalls=1)
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)

    seen_on_disk: list[bool] = []

    async def _capture_sleep(_delay: float, _coord: Any) -> None:
        seen_on_disk.append(State.load(change.state_dir).retry_continue_pending)

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _capture_sleep)

    result = await run_change(change)
    assert result is RunResult.COMPLETED
    assert provider.drives == 2               # one stall + one success
    # The marker was set AND persisted to disk BEFORE the backoff sleep ran.
    assert seen_on_disk == [True]


class _RecordingProvider:
    """Records invocation.resume_should_continue at each drive; stalls the first
    `stalls` drives, then succeeds."""

    name = "fake"
    model = "m"

    def __init__(self, stalls: int) -> None:
        self.stalls = stalls
        self.drives = 0
        self.seen: list[bool] = []

    def create_runner(self, inv: Any) -> _StallNthRunner:
        self.seen.append(inv.resume_should_continue)
        self.drives += 1
        return _StallNthRunner(self.drives <= self.stalls)


@pytest.mark.anyio
async def test_backoff_respawn_receives_resume_should_continue(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(
        tmp_path, agents={"writer.md": _WRITER}, workflow_yaml=_SINGLE_WF)
    provider = _RecordingProvider(stalls=1)
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)

    async def _no_sleep(_delay: float, _coord: Any) -> None:
        return None

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    result = await run_change(change)
    assert result is RunResult.COMPLETED
    # First drive: fresh state, marker False → resume_should_continue False.
    # Second drive (respawn after backoff): the union reads
    # retry_continue_pending with NO is_resume gate → True.
    assert provider.seen == [False, True]
