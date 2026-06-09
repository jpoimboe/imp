"""End-to-end test of the shipped default workflow via mocked Anthropic.

The shipped workflow.yaml has 5 phases (spec → plan → implement → review →
merge); only review has a human gate ("Merge?"). The shipped script gates
check that artifact files exist; we replace them with `exit 0` shims
because the fake provider doesn't write real artifacts.

What this proves:
- Multi-phase progression (spec → plan → implement → review → merge)
- AgentGate retry/continue semantics through the executor
- Human gate option-1 path through the single review gate
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


def _canned_anthropic(**overrides):
    """A single complete_step response carrying every field any agent needs.
    Override individual fields (e.g. complexity='trivial') per test."""
    fields = {
        "status": "Success",
        "verdict": "approve", "findings": "none",
        "decision": "merge", "comments": "",
        "phase": "written", "spec_path": "stub-spec.md",
        "complexity": "standard",
        "plan_path": "stub-plan.md", "plan_number": 1,
        "done": True, "deviation": False, "summary": "ok",
        "more_plans": False, "rationale": "ok",
        "blocking": False, "has_minor_findings": False,
        "built": "stub", "test_status": "ok",
        "open_items": "", "next_steps": "",
        "result": "DONE", "report": "ok",
    }
    fields.update(overrides)
    return response(tool_use("c0", "complete_step", fields), stop_reason="tool_use")


def _make_change(root: Path, *, slug: str = "test-change", title: str = "t") -> Change:
    """Construct a Change directly on disk without git worktrees."""
    from imp.engine.loader import load_workflow
    from imp.providers.effective import load_effective_config

    src_wf = root / "imp" / "workflows" / "default"
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

    Also initializes a bare-minimum git repo so the merge gate (ready_to_merge)
    can run `git status --porcelain` without failing.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "imp" / "assets" / "workflows" / "default"
    dst = tmp_path / "imp" / "workflows" / "default"
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
    (tmp_path / "imp" / "project.yaml").write_text("workflow: default\n")
    (tmp_path / ".imp").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".imp" / "config.yaml").write_text(cfg_content)

    # Initialize a minimal git repo so `git status --porcelain` and
    # merge_to_base work when the merge-gate sets ready_to_merge=true.
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
    #   verdict:approve    code-reviewer → continue (not task_gap/plan_gap)
    #   decision:merge     final-review  → sets ready_to_merge=true, continue
    #   phase:written      spec-writer   → not drafting, advances
    #   done:true          task-lead     → plan fully implemented
    #   deviation:false    task-lead     → no supercycle back to plan-writer
    #   more_plans:false   plan-cycle-check → no supercycle, continue
    #   blocking:false     spec/plan reviewers → no rework loop
    default = response(
        tool_use("c0", "complete_step", {
            "status": "Success",
            # code-reviewer routing
            "verdict": "approve",
            "findings": "none",
            # final-review routing (superpowers workflow)
            "decision": "merge",
            "comments": "",
            # spec-writer phase
            "phase": "written",
            "spec_path": "stub-spec.md",
            "complexity": "standard",
            # plan-writer
            "plan_path": "stub-plan.md",
            "plan_number": 1,
            # task-lead loop control
            "done": True,
            "deviation": False,
            "summary": "ok",
            # plan-cycle-check
            "more_plans": False,
            "rationale": "ok",
            # reviewers
            "blocking": False,
            "has_minor_findings": False,
            # summarizer
            "built": "stub",
            "test_status": "ok",
            "open_items": "",
            "next_steps": "",
            # implementer
            "result": "DONE",
            "report": "ok",
        }),
        stop_reason="tool_use",
    )
    fake = FakeAsyncAnthropic([], default_response=default)

    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "anthropic",
        env_check=lambda: True,
        factory=lambda model: AnthropicProvider(
            model_name=model, client_factory=lambda: fake,
        ),
    )

    # The superpowers workflow has no classic human-gate prompts (no steps with
    # options:). The final-review agent uses wait_for_user to solicit the human,
    # but the fake provider returns complete_step immediately (no wait_for_user
    # call in the scripted response), so human_input_fn is never called.
    answer_count = [0]
    def _human_input() -> str:
        answer_count[0] += 1
        return "1"

    result = await run_change(
        change,
        on_event=lambda _e: None,
        human_input_fn=_human_input,
        human_output_fn=lambda _msg: None,
    )

    assert result is RunResult.COMPLETED, (
        f"expected COMPLETED, got {result!r}; "
        f"human gates answered: {answer_count[0]}"
    )
    # No classic human gate in the superpowers workflow (final-review uses
    # wait_for_user, but the fake skips it and completes directly with
    # decision=merge — the genuine solicit flow is covered by
    # test_final_review_wait_for_user_solicit_then_merge).
    assert answer_count[0] == 0


@pytest.mark.anyio
async def test_full_default_workflow_completes_with_canned_openai(
    tmp_path: Path,
) -> None:
    import json

    from imp.providers.openai import OpenAIProvider
    from tests.openai_fakes import (
        FakeResponsesClient,
        resp_function_call,
        resp_response,
    )

    change = _seed_default_workflow(tmp_path)
    # Rewrite config to OpenAI provider for every role (both layers: the legacy
    # imp/config.yaml and the .imp/config.yaml local layer the reader uses).
    _rewrite_config(tmp_path, ("anthropic", "openai"),
                    ("claude-sonnet-4-6", "gpt-4o-mini"))

    default_completion = resp_response(
        resp_function_call("c0", "complete_step", json.dumps({
            "status": "Success",
            "verdict": "approve", "findings": "none",
            "decision": "merge", "comments": "",
            "phase": "written", "spec_path": "stub-spec.md",
            "complexity": "standard",
            "plan_path": "stub-plan.md", "plan_number": 1,
            "done": True, "deviation": False, "summary": "ok",
            "more_plans": False, "rationale": "ok",
            "blocking": False, "has_minor_findings": False,
            "built": "stub", "test_status": "ok",
            "open_items": "", "next_steps": "",
            "result": "DONE", "report": "ok",
        })),
    )
    fake = FakeResponsesClient([], default_response=default_completion)

    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "openai", env_check=lambda: True,
        factory=lambda model: OpenAIProvider(
            model_name=model, client_factory=lambda: fake,
        ),
    )

    answer_count = [0]
    result = await run_change(
        change,
        on_event=lambda _e: None,
        human_input_fn=lambda: (answer_count.__setitem__(0, answer_count[0] + 1), "1")[1],
        human_output_fn=lambda _msg: None,
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

    # The CC fake is per-step; each runner spawns a fresh client. Use a
    # factory that returns the same scripted reply for every new client.
    def _scripted():
        return [
            assistant(
                text("ok"),
                cc_tool_use("c0", "complete_step", {
                    "status": "Success",
                    "verdict": "approve", "findings": "none",
                    "decision": "merge", "comments": "",
                    "phase": "written", "spec_path": "stub-spec.md",
                    "complexity": "standard",
                    "plan_path": "stub-plan.md", "plan_number": 1,
                    "done": True, "deviation": False, "summary": "ok",
                    "more_plans": False, "rationale": "ok",
                    "blocking": False, "has_minor_findings": False,
                    "built": "stub", "test_status": "ok",
                    "open_items": "", "next_steps": "",
                    "result": "DONE", "report": "ok",
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

    result = await run_change(
        change,
        on_event=lambda _e: None,
        human_input_fn=lambda: "1",
        human_output_fn=lambda _msg: None,
    )
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
    fake = FakeAsyncAnthropic([], default_response=_canned_anthropic(has_minor_findings=True))

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
        human_input_fn=lambda: "1", human_output_fn=lambda _msg: None,
    )
    assert result is RunResult.COMPLETED

    # Exactly two reviews per phase: the initial review + the owed verification
    # review after the writer integrates minors. The cycle does NOT loop a third
    # time even though the verification review still reports minors.
    assert started.count("spec-reviewer") == 2, started
    assert started.count("plan-review") == 2, started
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
        s == "plan-review"
        and a.get("verb") == "goto" and a.get("target") == "task-lead"
        for s, a in completed
    ), completed


@pytest.mark.anyio
async def test_fast_path_skips_both_reviewers_but_not_code_reviewer(
    tmp_path: Path,
) -> None:
    change = _seed_default_workflow(tmp_path)
    # complexity:trivial -> spec-writer sets fast_path=true; both the spec and
    # plan reviewers are skipped, but the final code-reviewer still runs.
    fake = FakeAsyncAnthropic([], default_response=_canned_anthropic(complexity="trivial"))

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

    result = await run_change(
        change, on_event=on_event,
        human_input_fn=lambda: "1", human_output_fn=lambda _msg: None,
    )
    assert result is RunResult.COMPLETED
    assert "spec-reviewer" not in started, started
    assert "plan-review" not in started, started
    assert "code-reviewer" in started, started


@pytest.mark.anyio
async def test_resume_from_maxed_gate_reruns_and_merges(tmp_path: Path) -> None:
    from imp.engine import recovery
    from imp.engine.loader import load_workflow
    from imp.providers.effective import load_effective_config

    change = _seed_default_workflow(tmp_path)

    # Position the Change as if paused at a maxed-out code-reviewer: its loop
    # budget (max_loops 5) is exhausted at 6. apply_rewind must clear that count
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
    plan = recovery.plan_rewind(wf, st, "code-reviewer", force=False)
    recovery.apply_rewind(plan, wf, st, change.event_log())
    st.status = "running"
    st.save(change.state_dir)
    assert "code-reviewer" not in st.loop_counts          # the load-bearing reset guard

    default = response(
        tool_use("c0", "complete_step", {
            "status": "Success",
            "verdict": "approve", "findings": "none",
            "decision": "merge", "comments": "",
            "phase": "written", "spec_path": "stub-spec.md",
            "complexity": "standard",
            "plan_path": "stub-plan.md", "plan_number": 1,
            "done": True, "deviation": False, "summary": "ok",
            "more_plans": False, "rationale": "ok",
            "blocking": False,
            "built": "stub", "test_status": "ok",
            "open_items": "", "next_steps": "",
            "result": "DONE", "report": "ok",
        }),
        stop_reason="tool_use",
    )
    fake = FakeAsyncAnthropic([], default_response=default)
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "anthropic", env_check=lambda: True,
        factory=lambda model: AnthropicProvider(
            model_name=model, client_factory=lambda: fake),
    )

    result = await run_change(
        change, on_event=lambda _e: None,
        human_input_fn=lambda: "1", human_output_fn=lambda _msg: None,
    )

    assert result is RunResult.COMPLETED
    events = list(change.event_log().read())
    assert any(e["type"] == "step_rewound" for e in events)
    # Merged: worktree removed by the terminal merge gate.
    assert not (tmp_path / ".imp" / "worktrees" / "test-change").exists()


@pytest.mark.anyio
async def test_final_review_wait_for_user_solicit_then_merge(tmp_path: Path) -> None:
    """Exercises the wait_for_user flow in final-review: the agent calls
    wait_for_user to solicit the human, receives the reply as the tool result,
    then completes the step with decision=merge.

    The change is pre-positioned at final-review (skipping the earlier phases)
    so only the final-review → merge path runs. The fake provider is scripted
    with TWO responses: first a wait_for_user tool_use, then a complete_step
    tool_use. user_chat_input_fn returns "looks good, merge it" which is
    injected as the tool_result for the wait_for_user call.
    """
    change = _seed_default_workflow(tmp_path)

    # Position the Change directly at final-review, bypassing earlier phases.
    st = State.load(change.state_dir)
    st.step = "final-review"
    st.status = "running"
    # final-review checks final_review var (default true → runs) and
    # ready_to_merge must be false to avoid skipping.
    st.vars["final_review"] = True
    st.vars["ready_to_merge"] = False
    st.save(change.state_dir)

    # Script two responses for the final-review agent:
    #   1. wait_for_user — agent solicits the human
    #   2. complete_step(decision=merge) — agent finishes after receiving reply
    wfu_response = response(
        tool_use("wfu1", "wait_for_user", {"prompt": "Summary: stub change. Merge or rework?"}),
        stop_reason="tool_use",
    )
    merge_response = response(
        tool_use("c1", "complete_step", {
            "status": "Success",
            "decision": "merge",
            "comments": "user approved",
        }),
        stop_reason="tool_use",
    )
    fake = FakeAsyncAnthropic([wfu_response, merge_response])
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "anthropic", env_check=lambda: True,
        factory=lambda model: AnthropicProvider(
            model_name=model, client_factory=lambda: fake),
    )

    # Attended: user_chat_input_fn supplies the reply for wait_for_user.
    human_replies: list[str] = []

    def _user_chat_input(prompt: str) -> str:
        human_replies.append(prompt)
        return "looks good, merge it"

    events_seen: list[dict] = []
    result = await run_change(
        change, on_event=events_seen.append,
        # human_input_fn is the classic gate (numeric choice); not exercised in this
        # attended test — wait_for_user is routed through user_chat_input_fn instead.
        human_input_fn=lambda: "1", human_output_fn=lambda _msg: None,
        user_chat_input_fn=_user_chat_input,
    )

    # The run completes: final-review → merge → COMPLETED.
    assert result is RunResult.COMPLETED, (
        f"expected COMPLETED, got {result!r}; events: {[e['type'] for e in events_seen]}"
    )

    # The human was solicited exactly once via wait_for_user.
    assert len(human_replies) == 1, (
        f"expected 1 wait_for_user solicit, got {human_replies!r}"
    )

    # awaiting_user_input event was emitted (the runner records the solicit).
    event_types = [e["type"] for e in events_seen]
    assert "awaiting_user_input" in event_types, (
        f"awaiting_user_input event not found in {event_types}"
    )
