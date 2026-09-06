"""Mid-step go-live must re-root the sandbox policy on every re-entry path:
subagent dispatch after a retry, and a reincarnated session."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner
from imp.engine.executor import RunResult, _reconcile_sandbox_policy, run_quest
from imp.engine.quest import Quest
from imp.engine.reincarnation import handoff_path_for
from imp.engine.sandbox import SandboxPolicy
from imp.paths import project_state_dir
from imp.providers.scripted import ScriptedAgentRunner
from tests.conftest import _git, _init_repo
from tests.golive_fakes import GoLiveOnce, patch_policy_builder


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _seed_draft(root: Path, quest_id: str) -> Quest:
    """A committed one-step workflow (driver + helper subagent) in a real git
    repo, left as a DRAFT so the step can go live mid-invocation."""
    _init_repo(root)
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "driver.md").write_text(
        "---\nname: driver\nsubagents: [helper]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    (wf / "agents" / "helper.md").write_text(
        "---\nname: helper\ntools: [read_file]\n---\nhelp\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - {name: w, file: agents/driver.md, tier: low}\n"
    )
    (root / "imp" / "config.yaml").write_text(
        "default-workflow: imp\ndefault-base: main\n"
        "context:\n  max_reincarnations: 1\n"
    )
    (project_state_dir(root) / "config.yaml").write_text(
        "providers: [fake]\n"
        "tiers:\n"
        "  low: {fake: m}\n"
        "  medium: {fake: m}\n"
        "  high: {fake: m}\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup")
    return Quest.create_draft(
        root, title="t", quest_id=quest_id, base_branch="main")


@pytest.mark.anyio
async def test_subagent_after_go_live_retry_inherits_the_reconciled_policy(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The path by which the user's main checkout stayed writable: a subagent
    dispatched after a post-go-live retry must receive the worktree-rooted
    policy, not the draft one."""
    sink: list[AgentInvocation] = []
    roots: list[Path] = []
    fired: list[bool] = []

    class _P:
        name = "fake"
        model = "m"

        def create_runner(self, inv: AgentInvocation) -> AgentRunner:
            sink.append(inv)
            if inv.subagent_depth == 0:
                # Dispatch-time: completion resets quest.project_root. Guarded —
                # a child invocation carries no quest: _build_sub_inv passes none.
                roots.append(inv.quest.project_root)
            return GoLiveOnce(ScriptedAgentRunner(model_name="m"), fired,
                              fail_first=True)

    provider_registry("fake", env_check=lambda: True, factory=lambda _m: _P())

    quest = _seed_draft(tmp_path, "gl2")
    patch_policy_builder(monkeypatch)

    script = {"steps": {
        "driver": [
            {"text": "dispatching", "tools": [
                {"name": "sub_agent", "input": {
                    "subagent_name": "helper",
                    "task_description": "look around",
                    "tier": "medium"}}]},
            {"text": "done", "tools": [
                {"name": "complete_step",
                 "input": {"status": "Success", "outcome": "ok"}}]},
        ],
        "helper": [{"text": "helper deliverable"}],
    }}
    # Outside the repo tree so the control checkout stays clean.
    script_path = quest.quest_dir / "script.json"
    script_path.write_text(json.dumps(script))
    monkeypatch.setenv("IMP_SCRIPTED_SCRIPT", str(script_path))

    async def _no_sleep(_delay: float, _coordinator: Any) -> None:
        return None

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    out = await run_quest(quest)

    assert out is RunResult.COMPLETED
    wt = project_state_dir(tmp_path) / "worktrees" / "gl2"
    assert roots == [tmp_path, wt]     # go-live really forked the worktree
    subs = [i for i in sink if i.subagent_depth == 1]
    assert len(subs) == 1        # dispatched on the post-retry drive only
    assert subs[0].sandbox_policy.worktree == str(wt)
    assert subs[0].sandbox_policy.git_worktree_self == str(
        tmp_path / ".git" / "worktrees" / "gl2")


@pytest.mark.anyio
async def test_reincarnated_step_reenters_with_the_reconciled_policy(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The choke-point reconcile reaches the reincarnation re-entry too, with no
    per-site change at either reincarnation call."""
    class _HandoffRunner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Any]:
            hp = handoff_path_for(inv.transcript_dir)
            hp.parent.mkdir(parents=True, exist_ok=True)
            hp.write_text("# handoff\n")
            yield {"type": "handoff", "payload": {"handoff_path": str(hp)}}

        async def interrupt(self) -> None:
            return None

    sink: list[AgentInvocation] = []
    fired: list[bool] = []

    class _P:
        name = "fake"
        model = "m"

        def create_runner(self, inv: AgentInvocation) -> AgentRunner:
            sink.append(inv)
            return GoLiveOnce(_HandoffRunner(), fired)

    provider_registry("fake", env_check=lambda: True, factory=lambda _m: _P())

    quest = _seed_draft(tmp_path, "rc1")
    patch_policy_builder(monkeypatch)

    events: list[dict[str, Any]] = []
    out = await run_quest(quest, on_event=events.append)

    assert out is RunResult.PAUSED           # reincarnated once, then the cap
    assert [e["type"] for e in events].count("reincarnating") == 1
    assert len(sink) == 2
    wt = project_state_dir(tmp_path) / "worktrees" / "rc1"
    assert sink[0].sandbox_policy.worktree == str(tmp_path)
    assert sink[1].sandbox_policy.worktree == str(wt)
    assert sink[1].sandbox_policy.git_worktree_self == str(
        tmp_path / ".git" / "worktrees" / "rc1")


def test_draft_quest_reconcile_clears_git_worktree_self(tmp_path: Path) -> None:
    # A control-root-rooted quest must not be handed a worktree-self bind.
    quest = _seed_draft(tmp_path, "dr1")
    worktree_path = str(project_state_dir(tmp_path) / "worktrees" / "dr1")
    policy = SandboxPolicy(
        active=False,
        worktree=worktree_path,
        git_common_dir=str(tmp_path / ".git"),
        git_ro_remask=(),
        writable_caches=(),
        extra_readable=(),
        extra_writable=(),
        secret_dirs=(),
        secret_files=(),
        setenv=(),
        network=False,
        git_worktree_self=str(tmp_path / ".git" / "worktrees" / "dr1"),
    )
    reconciled = _reconcile_sandbox_policy(policy, quest)
    assert reconciled.worktree == str(quest.project_root)
    assert reconciled.git_worktree_self == ""
