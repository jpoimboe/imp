"""End-to-end test of the shipped default workflow via mocked Anthropic.

The shipped workflow.yaml has 5 phases (spec → plan → implement → review →
merge). The shipped script gates check that artifact files exist; we replace
them with `exit 0` shims because the fake provider doesn't write real artifacts.

What this proves:
- Multi-phase progression (spec → plan → implement → review → merge)
- AgentGate retry/continue semantics through the executor
- All shipped agent .md files parse and run via the executor
- Step-level script-gate evaluation chains correctly with phase-level
  agent gates
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from imp.engine.state import State
from imp.providers import resolve
from imp.providers.anthropic import AnthropicProvider
from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use
from tests.openai_fakes import resp_function_call, resp_response


def _canned_anthropic(**overrides):
    """A single complete_step response carrying every field any agent needs.
    Override individual fields (e.g. complexity='trivial') per test."""
    fields = {
        "outcome": "ok",
        "status": "Success",
        "verdict": "approve", "findings": "none",
        "decision": "merge", "comments": "",
        "phase": "written", "spec_path": "stub-spec.md",
        "complexity": "standard",
        "plan_path": "stub-plan.md", "plan_number": 1,
        "done": True, "deviation": False, "verify_tolerated": False,
        "sweep_tolerated": False, "summary": "ok",
        "more_plans": False, "running_tasks": 0, "rationale": "ok",
        "blocking": False, "has_minor_findings": False,
        "built": "stub", "verify_status": "ok",
        "open_items": "", "next_steps": "",
        "result": "DONE", "report": "ok",
        "integration": "clean",
    }
    fields.update(overrides)
    return response(tool_use("c0", "complete_step", fields), stop_reason="tool_use")


# Unique text that appears only in integration-review's rendered system prompt
# (the opening line of the agent body, also in the completion instruction).
_IR_SIG = "HEAD of the merge queue"


def _anthropic_step_aware(**kwargs):
    """Callable default for FakeAsyncAnthropic: returns integration-review-
    appropriate canned response (verdict: ok) when the system prompt signals
    that step; falls back to the standard response otherwise."""
    system = kwargs.get("system", "")
    if _IR_SIG in system:
        return _canned_anthropic(verdict="ok")
    return _canned_anthropic()


def _openai_step_aware(**kwargs):
    """Callable default for FakeResponsesClient: same step-awareness as
    _anthropic_step_aware but for the OpenAI responses API (instructions=)."""
    instructions = kwargs.get("instructions", "")
    verdict = "ok" if _IR_SIG in instructions else "approve"
    fields = {
        "outcome": "ok",
        "status": "Success",
        "verdict": verdict, "findings": "none",
        "decision": "merge", "comments": "",
        "phase": "written", "spec_path": "stub-spec.md",
        "complexity": "standard",
        "plan_path": "stub-plan.md", "plan_number": 1,
        "done": True, "deviation": False, "verify_tolerated": False,
        "sweep_tolerated": False, "summary": "ok",
        "more_plans": False, "running_tasks": 0, "rationale": "ok",
        "blocking": False, "has_minor_findings": False,
        "built": "stub", "verify_status": "ok",
        "open_items": "", "next_steps": "",
        "result": "DONE", "report": "ok",
        "integration": "clean",
    }
    return resp_response(resp_function_call("c0", "complete_step", json.dumps(fields)))


def _make_change(root: Path, *, change_id: str = "test-change", title: str = "t") -> Change:
    """Construct a Change directly on disk without git worktrees."""
    from imp.engine.loader import load_workflow
    from imp.providers.effective import load_effective_config

    src_wf = root / "imp" / "workflows" / "imp"
    change_dir = root / ".imp" / "change"
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "state").mkdir(exist_ok=True)
    (change_dir / "change.json").write_text(json.dumps({
        "id": change_id, "title": title, "base_branch": "main",
    }, indent=2, sort_keys=True))

    configured_tiers: list[str] = []
    with contextlib.suppress(Exception):
        configured_tiers = load_effective_config(
            project_root=root, control_root=root).config.tier_names()

    workflow = load_workflow(src_wf, configured_tiers=configured_tiers)
    flat = workflow.flat_steps()
    State(
        id=change_id, step=flat[0].name if flat else None, step_attempt=0,
        status="running", vars=dict(workflow.vars.defaults()), loop_counts={},
    ).save(change_dir / "state")

    change = Change(
        id=change_id, title=title, base_branch="main",
        control_root=root, project_root=root, change_dir=change_dir,
    )
    change.event_log().append(
        agent_id=None, type="change_created",
        payload={"id": change_id, "title": title},
    )
    return change


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
    )


def _seed_default_workflow(tmp_path: Path) -> Change:
    """Copy the shipped default workflow into the change root, then replace
    the script-gate shells with `exit 0` stubs.

    Also initializes a bare-minimum git repo so the merge gate
    can run `git status --porcelain` without failing.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "imp" / "assets" / "workflows" / "imp"
    dst = tmp_path / "imp" / "workflows" / "imp"
    shutil.copytree(src, dst)
    # Replace script gates with always-pass stubs.
    for shell in (dst / "scripts").glob("*.sh"):
        shell.write_text("#!/bin/sh\nexit 0\n")
        shell.chmod(0o755)

    # config: a single role for every tier used by the workflow. run_change reads
    # via load_effective_config (project layer from imp/project.yaml + local layer
    # from .imp/config.yaml). Tests 2/3 swap the provider via _rewrite_config.
    (tmp_path / "imp").mkdir(parents=True, exist_ok=True)
    cfg_content = (
        "providers: [anthropic]\n"
        "tiers:\n"
        "  low:    {anthropic: claude-sonnet-4-6}\n"
        "  medium: {anthropic: claude-sonnet-4-6}\n"
        "  high:   {anthropic: claude-sonnet-4-6}\n"
    )
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (tmp_path / ".imp").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".imp" / "config.yaml").write_text(cfg_content)

    # Initialize a minimal git repo so `git status --porcelain` and
    # merge_to_base work.
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    # Ignore both .imp/ (runtime state + local config layer) and imp/ (test-only
    # workflow/marker) so config rewrites in tests 2 and 3 don't dirty the worktree.
    (tmp_path / ".gitignore").write_text(".imp/\nimp/\n")
    (tmp_path / "README.md").write_text("seed\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    # Create the imp/<slug> branch and a git worktree at the expected path so
    # merge_to_base and remove_worktree both succeed.
    (tmp_path / ".imp" / "worktrees").mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "worktree", "add", "-q", "-b", "imp/test-change",
         ".imp/worktrees/test-change", "main")

    return _make_change(tmp_path, title="e2e-default-workflow")


def _rewrite_config(tmp_path: Path, *replacements: tuple[str, str]) -> None:
    """Apply str replacements to the .imp/config.yaml local layer (the one
    load_effective_config reads), so a provider swap in a test reaches the
    runtime."""
    p = tmp_path / ".imp" / "config.yaml"
    text = p.read_text()
    for old, new in replacements:
        text = text.replace(old, new)
    p.write_text(text)


@pytest.mark.anyio
async def test_full_default_workflow_completes_with_canned_anthropic(
    tmp_path: Path,
) -> None:
    change = _seed_default_workflow(tmp_path)

    # Default response: complete_step with ALL fields needed by any agent in the
    # superpowers workflow. The fake's default_response is consulted whenever the
    # canned list is empty.
    #
    # LOAD-BEARING: this strategy relies on `validate_agent_output` (in
    # src/imp/engine/schema.py:72) iterating `schema.fields` only — extra
    # keys in the output payload are silently ignored, not rejected. If a
    # future tightening makes the validator strict, this test breaks and
    # the fix is to construct per-agent canned responses instead.
    #
    # Fields that drive workflow routing (must produce a single-pass run):
    #   verdict:approve    review (code-reviewer) → continue (not task_gap)
    #   decision:merge     finalize  → goto merge
    #   phase:written      spec-writer   → not drafting, advances
    #   done:true          task-lead     → plan fully implemented
    #   deviation:false    task-lead     → no supercycle back to plan-writer
    #   more_plans:false   plan-cycle-check → no supercycle, continue
    #   blocking:false     spec/plan reviewers → no rework loop
    fake = FakeAsyncAnthropic([], default_response=_anthropic_step_aware)

    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "anthropic",
        env_check=lambda: True,
        factory=lambda model: AnthropicProvider(
            model_name=model, client_factory=lambda: fake,
        ),
    )

    # The finalize agent uses wait_for_user to solicit the human, but the fake
    # provider returns complete_step immediately (no wait_for_user call in the
    # scripted response), so the reply channel is never exercised.
    result = await run_change(
        change,
        on_event=lambda _e: None,
    )

    assert result is RunResult.COMPLETED, (
        f"expected COMPLETED, got {result!r}"
    )


@pytest.mark.anyio
async def test_full_default_workflow_completes_with_canned_openai(
    tmp_path: Path,
) -> None:
    from imp.providers.openai import OpenAIProvider
    from tests.openai_fakes import FakeResponsesClient

    change = _seed_default_workflow(tmp_path)
    # Rewrite config to OpenAI provider for every role (both layers: the legacy
    # imp/config.yaml and the .imp/config.yaml local layer the reader uses).
    _rewrite_config(tmp_path, ("anthropic", "openai"),
                    ("claude-sonnet-4-6", "gpt-4o-mini"))

    fake = FakeResponsesClient([], default_response=_openai_step_aware)

    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "openai", env_check=lambda: True,
        factory=lambda model: OpenAIProvider(
            model_name=model, client_factory=lambda: fake,
        ),
    )

    result = await run_change(
        change,
        on_event=lambda _e: None,
    )
    assert result is RunResult.COMPLETED


@pytest.mark.anyio
async def test_full_default_workflow_completes_with_canned_claude_code(
    tmp_path: Path,
) -> None:
    from typing import Any

    from imp.providers.claude_code import ClaudeCodeProvider
    from tests.cc_fakes import (
        FakeClaudeSDKClient,
        assistant,
        text,
    )
    from tests.cc_fakes import (
        result as cc_result,
    )
    from tests.cc_fakes import (
        tool_use as cc_tool_use,
    )

    change = _seed_default_workflow(tmp_path)
    _rewrite_config(tmp_path, ("anthropic", "claude_code"),
                    ("claude-sonnet-4-6", "claude-sonnet-4-5"))

    # The CC fake is per-step; each runner spawns a fresh client.
    # step_started fires before the factory is called, so cur_step["name"]
    # is already set to the running step when the factory executes.
    cur_step: dict[str, str] = {"name": ""}

    def _scripted():
        verdict = "ok" if cur_step["name"] == "integration-reviewer" else "approve"
        return [
            assistant(
                text("ok"),
                cc_tool_use("c0", "complete_step", {
                    "outcome": "ok",
                    "status": "Success",
                    "verdict": verdict, "findings": "none",
                    "decision": "merge", "comments": "",
                    "phase": "written", "spec_path": "stub-spec.md",
                    "complexity": "standard",
                    "plan_path": "stub-plan.md", "plan_number": 1,
                    "done": True, "deviation": False, "verify_tolerated": False,
                    "sweep_tolerated": False, "summary": "ok",
                    "more_plans": False, "running_tasks": 0, "rationale": "ok",
                    "blocking": False, "has_minor_findings": False,
                    "built": "stub", "verify_status": "ok",
                    "open_items": "", "next_steps": "",
                    "result": "DONE", "report": "ok",
                    "integration": "clean",
                }),
            ),
            cc_result(),
        ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=_scripted())

    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "claude_code", env_check=lambda: True,
        factory=lambda model: ClaudeCodeProvider(
            model_name=model, client_factory=factory,
        ),
    )

    def on_event(rec: dict) -> None:
        if rec.get("type") == "step_started":
            cur_step["name"] = rec.get("payload", {}).get("step", "")

    result = await run_change(change, on_event=on_event)
    assert result is RunResult.COMPLETED


@pytest.mark.anyio
async def test_clean_review_with_minors_integrates_then_verifies(
    tmp_path: Path,
) -> None:
    change = _seed_default_workflow(tmp_path)
    # standard change, every review clean (blocking:false) but always carrying
    # actionable minors (has_minor_findings:true). The new cycle: the first clean
    # review sends the writer back to integrate the minors (setting the
    # minors-integrated flag), then owes ONE verification review. That second
    # review is also clean, and because the flag is set it terminates and advances
    # to the next phase -- dropping its own minors rather than looping forever.
    def _default_with_minors(**kwargs):
        system = kwargs.get("system", "")
        if _IR_SIG in system:
            return _canned_anthropic(verdict="ok")
        return _canned_anthropic(has_minor_findings=True)

    fake = FakeAsyncAnthropic([], default_response=_default_with_minors)

    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "anthropic", env_check=lambda: True,
        factory=lambda model: AnthropicProvider(
            model_name=model, client_factory=lambda: fake,
        ),
    )

    started: list[str] = []
    completed: list[tuple[str, dict]] = []

    def on_event(rec: dict) -> None:
        if rec["type"] == "step_started":
            started.append(rec["payload"]["step"])
        elif rec["type"] == "step_completed":
            completed.append((rec["payload"]["step"], rec["payload"]["action"]))

    result = await run_change(
        change, on_event=on_event,
    )
    assert result is RunResult.COMPLETED

    # Exactly two reviews per phase: the initial review + the owed verification
    # review after the writer integrates minors. The cycle does NOT loop a third
    # time even though the verification review still reports minors.
    assert started.count("spec-reviewer") == 2, started
    assert started.count("plan-reviewer") == 2, started
    # The writer ran twice per phase: the initial write + the one minors pass.
    assert started.count("spec-writer") == 2, started
    assert started.count("plan-writer") == 2, started

    # Termination is the REVIEWER's goto on the verification pass (minors-integrated
    # already set), not a separate gate: the second spec-reviewer jumps to
    # plan-writer, the second plan-review jumps to task-lead.
    assert any(
        s == "spec-reviewer"
        and a.get("verb") == "goto" and a.get("target") == "plan-writer"
        for s, a in completed
    ), completed
    assert any(
        s == "plan-reviewer"
        and a.get("verb") == "goto" and a.get("target") == "task-lead"
        for s, a in completed
    ), completed


@pytest.mark.anyio
async def test_fast_path_skips_reviewers_and_review(
    tmp_path: Path,
) -> None:
    change = _seed_default_workflow(tmp_path)
    # complexity:trivial -> spec-writer sets fast_path=true. The fast path skips
    # spec-reviewer (A.1: spec-verify-commit routes straight to plan-writer with no
    # unseen change), plan-review (plan-verify-commit fast-path rule), and the whole
    # review -> deferred-sweep -> sweep-verify block (A.2: plan-cycle-check fast-path routes straight to finalize).
    # The run still reaches planning, implementation, and merge.
    def _default_trivial(**kwargs):
        system = kwargs.get("system", "")
        if _IR_SIG in system:
            return _canned_anthropic(verdict="ok")
        return _canned_anthropic(complexity="trivial")

    fake = FakeAsyncAnthropic([], default_response=_default_trivial)

    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "anthropic", env_check=lambda: True,
        factory=lambda model: AnthropicProvider(
            model_name=model, client_factory=lambda: fake,
        ),
    )

    started: list[str] = []

    def on_event(rec: dict) -> None:
        if rec["type"] == "step_started":
            started.append(rec["payload"]["step"])

    result = await run_change(change, on_event=on_event)
    assert result is RunResult.COMPLETED
    assert "spec-reviewer" not in started, started    # A.1: skipped on fast path
    assert "plan-reviewer" not in started, started     # plan-verify-commit fast-path rule
    assert "code-reviewer" not in started, started     # A.2: skipped on fast path
    assert "deferred-sweep" not in started, started    # A.2
    assert "sweep-verify" not in started, started      # A.2
    assert "plan-writer" in started, started           # planning still runs
    assert "task-lead" in started, started             # implementation still runs


@pytest.mark.anyio
async def test_resume_from_maxed_gate_reruns_and_merges(tmp_path: Path) -> None:
    from imp.engine import recovery
    from imp.engine.loader import load_workflow
    from imp.providers.effective import load_effective_config

    change = _seed_default_workflow(tmp_path)

    # Position the Change as if paused at a maxed-out review: its loop
    # budget (max_loops 6) is exhausted at 7. apply_rewind must clear that count
    # so a re-entry that loops back (a reject verdict) gets a fresh budget rather
    # than tripping the max_loops re-pause again. (On the approve path below the
    # gate advances via Continue, which pops the count regardless — so the reset
    # is guarded by the explicit assert on st.loop_counts, not by the COMPLETED
    # outcome; the runtime reset semantics are unit-tested in test_recovery.py.)
    st = State.load(change.state_dir)
    st.step = "code-reviewer"
    st.status = "paused"
    st.loop_counts = {"code-reviewer": 6}
    st.save(change.state_dir)

    wf = load_workflow(change.workflow_dir,
                       configured_tiers=load_effective_config(
                           project_root=tmp_path,
                           control_root=tmp_path).config.tier_names())
    plan = recovery.plan_rewind(wf, st, "code-reviewer")
    recovery.apply_rewind(plan, wf, st, change.event_log())
    st.status = "running"
    st.save(change.state_dir)
    assert "code-reviewer" not in st.loop_counts   # the load-bearing reset guard

    fake = FakeAsyncAnthropic([], default_response=_anthropic_step_aware)
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "anthropic", env_check=lambda: True,
        factory=lambda model: AnthropicProvider(
            model_name=model, client_factory=lambda: fake),
    )

    result = await run_change(
        change, on_event=lambda _e: None,
    )

    assert result is RunResult.COMPLETED
    events = list(change.event_log().read())
    assert any(e["type"] == "step_rewound" for e in events)
    # Merged: worktree removed by the terminal merge gate.
    assert not (tmp_path / ".imp" / "worktrees" / "test-change").exists()


@pytest.mark.anyio
async def test_finalize_wait_for_user_solicit_then_merge(tmp_path: Path) -> None:
    """finalize solicits the human via wait_for_user, receives the reply as the
    tool result, then completes with decision=merge. Positioned directly at
    finalize so only the finalize -> merge path runs."""
    change = _seed_default_workflow(tmp_path)
    st = State.load(change.state_dir)
    st.step = "finalize"
    st.status = "running"
    st.save(change.state_dir)

    wfu_response = response(
        tool_use("wfu1", "wait_for_user", {"prompt": "Summary: stub change. Merge or rework?"}),
        stop_reason="tool_use",
    )
    merge_response = response(
        tool_use("c1", "complete_step", {
            "outcome": "ok", "status": "Success", "decision": "merge", "comments": "user approved",
            "built": "stub", "verify_status": "ok", "open_items": "", "next_steps": "",
        }),
        stop_reason="tool_use",
    )
    fake = FakeAsyncAnthropic(
        [wfu_response, merge_response], default_response=_anthropic_step_aware)
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "anthropic", env_check=lambda: True,
        factory=lambda model: AnthropicProvider(
            model_name=model, client_factory=lambda: fake),
    )
    human_replies: list[str] = []

    def _user_chat_input(prompt: str) -> str:
        human_replies.append(prompt)
        return "looks good, merge it"

    events_seen: list[dict] = []
    result = await run_change(
        change, on_event=events_seen.append, user_chat_input_fn=_user_chat_input,
    )
    assert result is RunResult.COMPLETED, (
        f"expected COMPLETED, got {result!r}; events: {[e['type'] for e in events_seen]}"
    )
    assert len(human_replies) == 1, human_replies
    assert "awaiting_user_input" in [e["type"] for e in events_seen]


@pytest.mark.anyio
async def test_clean_rebase_skips_integrator_then_merges(tmp_path: Path) -> None:
    """A clean rebase (the stubbed integrate-rebase.sh exits 0): the integrator is
    SKIPPED; the merge tail is finalize → wait-for-head → integrate-rebase →
    integration-reviewer → integration-verify → merge-to-base."""
    change = _seed_default_workflow(tmp_path)
    fake = FakeAsyncAnthropic([], default_response=_anthropic_step_aware)
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "anthropic",
        env_check=lambda: True,
        factory=lambda model: AnthropicProvider(
            model_name=model, client_factory=lambda: fake,
        ),
    )
    result = await run_change(change, non_interactive=True)
    assert result is RunResult.COMPLETED
    steps = [e["payload"].get("step") for e in change.event_log().read()
             if e.get("type") == "step_started" and e.get("payload", {}).get("step")]
    for name in ("finalize", "wait-for-head", "integrate-rebase",
                 "integration-reviewer", "integration-verify", "merge-to-base"):
        assert name in steps, f"{name} did not run"
    assert "integrator" not in steps, "clean rebase must not call the integrator"
    assert (steps.index("finalize")
            < steps.index("wait-for-head")
            < steps.index("integrate-rebase")
            < steps.index("integration-reviewer")
            < steps.index("integration-verify")
            < steps.index("merge-to-base"))


@pytest.mark.anyio
async def test_conflict_rebase_routes_through_integrator(tmp_path: Path) -> None:
    """A conflict rebase (integrate-rebase.sh exits 6) routes through the
    integrator before integration-reviewer → integration-verify → merge-to-base."""
    change = _seed_default_workflow(tmp_path)
    ir = (change.project_root / "imp" / "workflows" / "imp"
          / "scripts" / "integrate-rebase.sh")
    ir.write_text("#!/usr/bin/env bash\nexit 6\n")
    ir.chmod(0o755)
    fake = FakeAsyncAnthropic([], default_response=_anthropic_step_aware)
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "anthropic",
        env_check=lambda: True,
        factory=lambda model: AnthropicProvider(
            model_name=model, client_factory=lambda: fake,
        ),
    )
    result = await run_change(change, non_interactive=True)
    assert result is RunResult.COMPLETED
    steps = [e["payload"].get("step") for e in change.event_log().read()
             if e.get("type") == "step_started" and e.get("payload", {}).get("step")]
    for name in ("integrate-rebase", "integrator", "integration-reviewer",
                 "integration-verify", "merge-to-base"):
        assert name in steps, f"{name} did not run"
    assert (steps.index("integrate-rebase")
            < steps.index("integrator")
            < steps.index("integration-reviewer")
            < steps.index("integration-verify")
            < steps.index("merge-to-base"))


@pytest.mark.anyio
async def test_merge_phase_runs_merge_to_base_step(tmp_path: Path) -> None:
    change = _seed_default_workflow(tmp_path)
    fake = FakeAsyncAnthropic([], default_response=_anthropic_step_aware)
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "anthropic",
        env_check=lambda: True,
        factory=lambda model: AnthropicProvider(
            model_name=model, client_factory=lambda: fake,
        ),
    )
    result = await run_change(change, non_interactive=True)
    assert result is RunResult.COMPLETED
    types_seen = [e.get("type") for e in change.event_log().read()]
    steps = [e["payload"].get("step") for e in change.event_log().read()
             if e.get("type") == "step_started"]
    assert "wait-for-head" in steps
    assert "merge-to-base" in steps
    assert "change_merged" in types_seen


@pytest.mark.anyio
async def test_merge_conflict_surfaces_merge_blocked(tmp_path: Path, monkeypatch) -> None:
    """A blocking outcome at merge-to-base must preserve the pre-B3 surfacing
    (review finding #2): status=='merge_blocked' AND a merge_blocked event, so the
    CLI remediation path stays alive — NOT a generic 'paused'."""
    change = _seed_default_workflow(tmp_path)
    from imp.engine import worktree as _wt
    from imp.engine.worktree import MergeResult
    monkeypatch.setattr(_wt, "merge_to_base",
                        lambda *a, **k: MergeResult("CONFLICT", detail="boom"))
    fake = FakeAsyncAnthropic([], default_response=_anthropic_step_aware)
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "anthropic",
        env_check=lambda: True,
        factory=lambda model: AnthropicProvider(
            model_name=model, client_factory=lambda: fake,
        ),
    )
    result = await run_change(change, non_interactive=True)
    assert result is RunResult.PAUSED
    final = State.load(change.state_dir)
    assert final.status == "merge_blocked"
    types_seen = [e.get("type") for e in change.event_log().read()]
    assert "merge_blocked" in types_seen


@pytest.mark.anyio
async def test_autonomous_mode_integrates_and_merges_without_human(tmp_path: Path) -> None:
    # No workflow var override needed — finalize merges without a human gate.
    change = _seed_default_workflow(tmp_path)
    fake = FakeAsyncAnthropic([], default_response=_anthropic_step_aware)
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "anthropic",
        env_check=lambda: True,
        factory=lambda model: AnthropicProvider(
            model_name=model, client_factory=lambda: fake,
        ),
    )
    result = await run_change(change, non_interactive=True)
    assert result is RunResult.COMPLETED
    steps = [e["payload"].get("step") for e in change.event_log().read()
             if e.get("type") == "step_started"]
    assert "integrate-rebase" in steps and "finalize" in steps
    assert "change_merged" in [e.get("type") for e in change.event_log().read()]


@pytest.mark.anyio
async def test_full_run_transcript_hides_quiet_plumbing(tmp_path: Path) -> None:
    """The RENDERED transcript of a full real-workflow run must not leak quiet
    plumbing: no `goto(...)` raw control-flow, and no quiet step rendered as its
    own block. The other e2e tests assert step PROGRESSION (the event log); this
    one renders the events the way `imp log` / the live driver do and checks what
    a human actually sees. Catches the class of regression that the unit render
    tests structurally can't — a quiet step REACHING a phase and emitting a
    routing action the renderer must humanize (e.g. integrate-rebase's
    goto(finalize) on its base-already-integrated no-op path)."""
    from imp.cli import narrative as nv
    from imp.cli.render_text import render_tree
    from imp.engine.loader import load_workflow

    change = _seed_default_workflow(tmp_path)
    fake = FakeAsyncAnthropic([], default_response=_anthropic_step_aware)
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "anthropic", env_check=lambda: True,
        factory=lambda model: AnthropicProvider(
            model_name=model, client_factory=lambda: fake,
        ),
    )
    result = await run_change(change, non_interactive=True)
    assert result is RunResult.COMPLETED

    events = list(change.event_log().read())          # the same source `imp log` renders
    body = list(render_tree(nv.build_tree(events), full=False, color=False, width=100))

    # Every quiet step is HIDDEN: it never renders its own block — neither a header
    # line (`  <name>`) nor a completion breadcrumb (`  <name> complete · …`). That
    # breadcrumb is exactly what the integrate-rebase regression produced
    # (`integrate-rebase complete · goto(summarizer)`). We check the block, NOT bare
    # name presence, because a LOUD step legitimately names a quiet step as a goto
    # TARGET (`review complete · goto(integrate-rebase)`) — and a loud agent
    # step showing its own routing breadcrumb is intended; humanize-routing is a
    # plumbing rule.
    quiet = {s.name for s in load_workflow(change.workflow_dir).flat_steps() if s.quiet}
    assert quiet, "expected the default workflow to mark plumbing steps quiet"
    for name in sorted(quiet):
        assert not any(ln.strip() == name for ln in body), (
            f"quiet step {name!r} rendered a header line:\n" + "\n".join(body))
        assert not any(f"{name} · complete" in ln for ln in body), (
            f"quiet step {name!r} rendered a completion breadcrumb:\n" + "\n".join(body))

    # No raw control-flow leaks: humanize-routing replaces goto(<step>) / [resume]
    # with the arrow forms on the loud breadcrumbs.
    assert not any("goto(" in ln for ln in body), (
        "raw goto(...) leaked into the transcript:\n" + "\n".join(body))
    assert not any("[resume]" in ln for ln in body), (
        "raw [resume] annotation leaked into the transcript:\n" + "\n".join(body))
    # A loud clean forward goto is UNLABELED -> silent: spec-reviewer (label:
    # "reviewing spec") renders only its gray completion line, with no own-line "→ …".
    spec_rev_i = next((i for i, ln in enumerate(body) if "reviewing spec · complete" in ln), None)
    assert spec_rev_i is not None, (
        "expected the spec-reviewer completion breadcrumb:\n" + "\n".join(body))
    # The line after the spec-reviewer completion must not be an own-line route label.
    if spec_rev_i + 1 < len(body):
        assert not body[spec_rev_i + 1].strip().startswith("→"), (
            "unlabeled spec-reviewer goto must not produce a route line:\n" + "\n".join(body))

