"""run_change's last-resort guard: an engine exception during dispatch is caught, logged,
and left resumable (PAUSED); a cancellation still propagates uncaught."""

from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from imp.engine.state import State


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class EngineBoom(Exception):
    """Stand-in for an engine bug raised during dispatch."""


def _make_change(root: Path) -> Change:
    from imp.engine.loader import load_workflow
    from imp.providers.effective import load_effective_config

    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    (root / "imp").mkdir(parents=True, exist_ok=True)
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(
        "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n"
    )

    quest_dir = root / ".imp" / "change"
    quest_dir.mkdir(parents=True, exist_ok=True)
    (quest_dir / "state").mkdir(exist_ok=True)
    (quest_dir / "change.json").write_text(json.dumps(
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
    ).save(quest_dir / "state")

    change = Change(
        id="c", title="t", base_branch="main",
        control_root=root, project_root=root, quest_dir=quest_dir,
    )
    change.event_log().append(
        agent_id=None, type="change_created", payload={"id": "c", "title": "t"})
    return change


@pytest.mark.anyio
async def test_internal_error_pauses_and_logs_instead_of_dying(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(tmp_path)
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: object())

    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise EngineBoom("project has no fact 'guidance'")

    monkeypatch.setattr("imp.engine.executor._dispatch_step", _boom)

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)

    assert result is RunResult.PAUSED

    types = [e["type"] for e in events]
    err_idx = max(i for i, t in enumerate(types) if t == "error")
    err = events[err_idx]["payload"]
    assert err["category"] == "internal"
    assert err["kind"] == "EngineBoom"
    assert err["message"] == "project has no fact 'guidance'"
    assert err["detail"]["traceback_tail"].strip() != ""
    # the workflow_paused immediately follows the error event
    assert types[err_idx + 1] == "workflow_paused"
    paused = events[err_idx + 1]["payload"]
    assert paused["reason"] == "internal_error"
    assert paused["kind"] == "EngineBoom"

    state = State.load(change.state_dir)
    assert state.status == "paused"
    assert state.resume_pending == "w"   # the interrupted step, armed for replay


@pytest.mark.anyio
async def test_cancellation_propagates_uncaught(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(tmp_path)
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: object())

    cancelled_cls = anyio.get_cancelled_exc_class()

    async def _cancel(*_a: Any, **_k: Any) -> Any:
        raise cancelled_cls()

    monkeypatch.setattr("imp.engine.executor._dispatch_step", _cancel)

    events: list[dict] = []
    with pytest.raises(cancelled_cls):
        await run_change(change, on_event=events.append)

    # The guard must NOT convert a cancellation into a pause.
    assert not any(
        e["type"] == "workflow_paused" and e["payload"].get("reason") == "internal_error"
        for e in events)


@pytest.mark.anyio
async def test_internal_error_surfaces_subprocess_stderr(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _make_change(tmp_path)
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: object())

    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise subprocess.CalledProcessError(
            returncode=128,
            cmd=["git", "commit-tree"],
            stderr="fatal: bad object refs/imp/artifacts/bonu10\n",
        )

    monkeypatch.setattr("imp.engine.executor._dispatch_step", _boom)

    events: list[dict] = []
    result = await run_change(change, on_event=events.append)

    assert result is RunResult.PAUSED

    err = next(
        e["payload"] for e in events
        if e["type"] == "error" and e["payload"].get("category") == "internal"
    )
    assert err["kind"] == "CalledProcessError"
    assert err["message"].endswith(
        " — fatal: bad object refs/imp/artifacts/bonu10")
    assert "fatal: bad object" in err["detail"]["stderr_tail"]
    assert err["detail"]["traceback_tail"].strip() != ""
