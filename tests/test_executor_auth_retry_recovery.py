"""End-to-end: an auth failure retried through the _route_agent backoff loop
recovers on drive 2 (no pause, no empty user prompt)."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner
from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from imp.engine.state import State
from imp.providers.claude_code import ClaudeCodeAgentRunner
from tests.cc_fakes import FakeClaudeSDKClient, assistant, result, text, tool_use


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _seed_single_agent(root: Path) -> Change:
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
        "providers: [fake]\n"
        "tiers:\n"
        "  low: {fake: m}\n"
        "  medium: {fake: m}\n"
        "  high: {fake: m}\n"
    )

    change_dir = root / ".imp" / "change"
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "state").mkdir(exist_ok=True)
    (change_dir / "change.json").write_text(json.dumps(
        {"id": "c", "title": "t", "base_branch": "main"}, indent=2, sort_keys=True,
    ))

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
        agent_id=None, type="change_created",
        payload={"id": "c", "title": "t"},
    )
    return change


@pytest.mark.anyio
async def test_auth_retry_recovers_through_backoff_loop(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    change = _seed_single_agent(tmp_path)

    scripts = [
        [assistant(text("Failed to authenticate. API Error: 401 "
                        "Invalid authentication credentials")),
         result(is_error=True, api_error_status=401)],
        [assistant(text("done"),
                   tool_use("c1", "complete_step",
                            {"status": "Success", "outcome": "done"})),
         result()],
    ]
    calls: dict[str, int] = {"n": 0}

    def client_factory(opts: Any) -> FakeClaudeSDKClient:
        script = scripts[calls["n"]]
        calls["n"] += 1
        return FakeClaudeSDKClient(opts, scripted_messages=script)

    class _P:
        name = "fake"
        model = "m"

        def create_runner(self, _inv: AgentInvocation) -> AgentRunner:
            return ClaudeCodeAgentRunner(
                model_name="m", client_factory=client_factory)

    provider_registry("fake", env_check=lambda: True, factory=lambda _m: _P())

    async def _no_sleep(_delay: float, _coordinator: Any) -> None:
        return None

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    out = await run_change(change)

    assert out is RunResult.COMPLETED
    assert State.load(change.state_dir).status == "completed"
    assert calls["n"] == 2

    log_types = [e["type"] for e in change.event_log().read()]
    assert "agent_retry" in log_types
    assert "awaiting_user_input" not in log_types
    assert "llm_response" in log_types
