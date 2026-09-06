"""End-to-end: an auth failure retried through the _route_agent backoff loop
recovers on drive 2 (no pause, no empty user prompt)."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner
from imp.engine.executor import RunResult, run_quest
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.paths import project_state_dir
from imp.providers import claude_code
from imp.providers import claude_token_refresh as ctr
from imp.providers.claude_code import ClaudeCodeAgentRunner
from tests.cc_fakes import FakeClaudeSDKClient, assistant, result, text, tool_use
from tests.golive_fakes import GoLiveOnce, patch_policy_builder


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _seed_single_agent(root: Path) -> Quest:
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
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    (root / "imp").mkdir(parents=True, exist_ok=True)
    (root / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    (project_state_dir(root) / "config.yaml").write_text(
        "providers: [fake]\n"
        "tiers:\n"
        "  low: {fake: m}\n"
        "  medium: {fake: m}\n"
        "  high: {fake: m}\n"
    )

    quest_dir = project_state_dir(root) / "quests" / "c"
    quest_dir.mkdir(parents=True, exist_ok=True)
    (quest_dir / "state").mkdir(exist_ok=True)
    (quest_dir / "quest.json").write_text(json.dumps(
        {"id": "c", "title": "t", "base_branch": "main"}, indent=2, sort_keys=True,
    ))

    configured_tiers: list[str] = []
    with contextlib.suppress(Exception):
        configured_tiers = load_effective_config(control_root=root).config.tier_names()

    workflow = load_workflow(wf, configured_tiers=configured_tiers)
    flat = workflow.flat_steps()
    State(
        id="c", step=flat[0].name if flat else None, step_attempt=0,
        status="running", vars=dict(workflow.vars.defaults()), loop_counts={},
    ).save(quest_dir / "state")

    quest = Quest(
        id="c", title="t", base_branch="main",
        control_root=root, project_root=root, quest_dir=quest_dir,
    )
    quest.event_log().append(
        agent_id=None, type="quest_created",
        payload={"id": "c", "title": "t"},
    )
    return quest


def _seed_draft_quest(root: Path, quest_id: str) -> Quest:
    """The same one-step workflow as _seed_single_agent, committed into a real
    git repo and left as a DRAFT so the step can go live mid-invocation."""
    from tests.conftest import _git, _init_repo

    _init_repo(root)
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [create_and_enter_worktree, complete_step]\n"
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
    (root / "imp" / "config.yaml").write_text(
        "default-workflow: imp\ndefault-base: main\n")
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
async def test_auth_retry_recovers_through_backoff_loop(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _seed_single_agent(tmp_path)

    scripts = [
        [assistant(text("API Error: 401 rejected by gateway")),
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

    out = await run_quest(quest)

    assert out is RunResult.COMPLETED
    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)  # home reaped on done
    assert State.load(quest.state_dir).status == "done"
    assert calls["n"] == 2

    events = list(quest.event_log().read())
    log_types = [e["type"] for e in events]
    assert "agent_retry" in log_types
    assert "awaiting_user_input" not in log_types
    assert "agent_message" in log_types

    prose = "API Error: 401 rejected by gateway"
    retries = [e for e in events if e["type"] == "agent_retry"]
    assert retries[0]["payload"]["message"] == prose
    # Bare 401 with no terminal prose must stay transient auth (retry-with-backoff).
    assert retries[0]["payload"]["category"] == "auth"

    # Verify from journaled rows: prose on the breadcrumb is engine chrome, not agent narration.
    from imp.cli import narrative as nv
    from imp.cli.render_text import render_tree
    # `fake` has no parser so build_tree raises; provider_error driving suppression is in the rows.
    rendered = [
        {**e, "payload": {**e["payload"], "provider": "claude_code"}}
        if e["type"] == "agent_message" else e
        for e in events
    ]
    tree = nv.build_tree(rendered)

    def _texts(node: Any) -> list[str]:
        found: list[str] = []
        if isinstance(node, nv.AgentTextNode):
            found.append(node.text)
        for c in getattr(node, "children", []):
            found.extend(_texts(c))
        return found

    assert prose not in _texts(tree.root)
    out = "\n".join(render_tree(tree, full=True, color=False, width=160))
    assert f"[auth] retry 1 (1.0s) — {prose}" in out
    # …and the healed first retry stays out of the lean transcript entirely.
    lean = "\n".join(render_tree(tree, full=False, color=False, width=160))
    assert "[auth] retry 1" not in lean


@pytest.mark.anyio
async def test_concurrency_400_retry_recovers_through_backoff_loop(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _seed_single_agent(tmp_path)

    scripts = [
        [assistant(text("API Error 400 — tool use concurrency")),
         result(is_error=True, api_error_status=400)],
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

    out = await run_quest(quest)

    assert out is RunResult.COMPLETED
    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)  # home reaped on done
    assert State.load(quest.state_dir).status == "done"
    assert calls["n"] == 2

    log_types = [e["type"] for e in quest.event_log().read()]
    assert "agent_retry" in log_types
    assert "workflow_paused" not in log_types
    assert "agent_message" in log_types


@pytest.mark.parametrize("readable_expiry", [False, True],
                         ids=["expiry-unreadable", "expiry-unmoved"])
@pytest.mark.anyio
async def test_dead_grant_probe_pauses_on_the_first_401(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
    readable_expiry: bool,
) -> None:
    """A dead grant pauses on the first 401 whether or not the canonical expiry
    is readable — an unmoved expiry is not a rotation race."""
    quest = _seed_single_agent(tmp_path)

    if readable_expiry:
        # The autouse ANTHROPIC_API_KEY stub blanks every canonical read, so
        # without this the run only exercises the guard's unreadable arm.
        canonical = tmp_path / "canonical"
        canonical.mkdir()
        (canonical / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {
                "accessToken": "x", "refreshToken": "y",
                "expiresAt": 1_000_000_000_000 + 3600 * 1000}}),
            encoding="utf-8")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(canonical))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    script = [
        assistant(text(
            "Failed to authenticate. API Error: 401 "
            "OAuth access token has been revoked.")),
        result(),
    ]

    def client_factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=script)

    class _P:
        name = "fake"
        model = "m"

        def create_runner(self, _inv: AgentInvocation) -> AgentRunner:
            return ClaudeCodeAgentRunner(
                model_name="m", client_factory=client_factory)

    provider_registry("fake", env_check=lambda: True, factory=lambda _m: _P())

    def _dead_on_force(now_ms: int, *, env: Any = None, force: bool = False,
                       spawn_expires_at: int | None = None) -> ctr.RefreshOutcome:
        if not force:
            return ctr.RefreshOutcome(ctr.NOT_APPLICABLE)
        return ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT)

    monkeypatch.setattr(claude_code, "refresh_canonical_token", _dead_on_force)

    async def _no_sleep(_delay: float, _coordinator: Any) -> None:
        return None

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    out = await run_quest(quest)

    assert out is RunResult.PAUSED
    events = list(quest.event_log().read())
    # The probe settles it on the first 401: no backoff, no retry, no ceiling.
    assert [e for e in events if e["type"] == "agent_retry"] == []
    paused = [e for e in events if e["type"] == "workflow_paused"]
    assert len(paused) == 1
    assert paused[0]["payload"]["reason"] == "reauth_needed"
    assert paused[0]["payload"]["kind"] == "AuthTerminalError"


@pytest.mark.anyio
async def test_retry_after_go_live_rebinds_policy_to_the_worktree(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A step that goes live and is then retried through the backoff loop must
    re-enter with its policy re-rooted at the new worktree — the window in which
    a live agent's sandbox still pointed at the user's main checkout."""
    scripts = [
        [assistant(text("API Error: 401 rejected by gateway")),
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

    sink: list[AgentInvocation] = []
    roots: list[Path] = []
    fired: list[bool] = []

    class _P:
        name = "fake"
        model = "m"

        def create_runner(self, inv: AgentInvocation) -> AgentRunner:
            sink.append(inv)
            # Dispatch-time: completion resets quest.project_root to the control root.
            roots.append(inv.quest.project_root)
            return GoLiveOnce(
                ClaudeCodeAgentRunner(model_name="m",
                                      client_factory=client_factory), fired)

    provider_registry("fake", env_check=lambda: True, factory=lambda _m: _P())

    quest = _seed_draft_quest(tmp_path, "gl1")
    patch_policy_builder(monkeypatch)

    async def _no_sleep(_delay: float, _coordinator: Any) -> None:
        return None

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    out = await run_quest(quest)

    assert out is RunResult.COMPLETED
    assert calls["n"] == 2
    assert len(sink) == 2
    wt = project_state_dir(tmp_path) / "worktrees" / "gl1"
    assert roots == [tmp_path, wt]     # go-live really forked the worktree
    # Drive 1 was dispatched with the pre-go-live policy…
    assert sink[0].sandbox_policy.worktree == str(tmp_path)
    # …and the retry re-entered with it re-rooted at the worktree.
    assert sink[1].sandbox_policy.worktree == str(wt)
    assert sink[1].sandbox_policy.git_worktree_self == str(
        tmp_path / ".git" / "worktrees" / "gl1")


@pytest.mark.anyio
async def test_retry_without_go_live_passes_the_policy_through_unchanged(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A quest that never materializes mid-invocation gets the SAME policy
    object back on re-entry: no replace, no rebuild, no emitted event."""
    scripts = [
        [assistant(text("API Error: 401 rejected by gateway")),
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

    sink: list[AgentInvocation] = []

    class _P:
        name = "fake"
        model = "m"

        def create_runner(self, inv: AgentInvocation) -> AgentRunner:
            sink.append(inv)
            return ClaudeCodeAgentRunner(
                model_name="m", client_factory=client_factory)

    provider_registry("fake", env_check=lambda: True, factory=lambda _m: _P())

    quest = _seed_single_agent(tmp_path)
    builds = patch_policy_builder(monkeypatch)

    async def _no_sleep(_delay: float, _coordinator: Any) -> None:
        return None

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    out = await run_quest(quest)

    assert out is RunResult.COMPLETED
    assert len(sink) == 2
    assert sink[1].sandbox_policy is sink[0].sandbox_policy   # same object: no replace
    assert builds["n"] == 1      # built once at run start; never rebuilt
