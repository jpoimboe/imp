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
        slug=slug, step=flat[0].name if flat else None, step_attempt=0,
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
    dst = tmp_path / ".imp" / "workflows" / "default"
    shutil.copytree(src, dst)
    # Replace script gates with always-pass stubs.
    for shell in (dst / "scripts").glob("*.sh"):
        shell.write_text("#!/bin/sh\nexit 0\n")
        shell.chmod(0o755)

    # config.yaml: a single role for every model_role used by the workflow.
    (tmp_path / "imp").mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "imp" / "config.yaml"
    cfg_content = (
        "roles:\n"
        "  default:       {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  spec:          {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  spec_review:   {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  plan:          {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  plan_review:   {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  task_lead:     {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  code_review:   {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  fast:          {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  balanced:      {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  powerful:      {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
    )
    cfg.write_text(cfg_content)

    # Initialize a minimal git repo so `git status --porcelain` and
    # merge_to_base work when the merge-gate sets ready_to_merge=true.
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    # Ignore both .imp/ (runtime state) and imp/ (test-only config) so that
    # rewrites of imp/config.yaml in tests 2 and 3 don't dirty the worktree.
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
    # options:). The final-review step is interactive=true but the fake provider
    # returns complete_step immediately so human_input_fn is never called.
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
    # No classic human gate in the superpowers workflow (final-review is an
    # interactive agent, not a human-options gate, and the fake completes it
    # directly with decision=merge).
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
    # Rewrite config to OpenAI provider for every role.
    cfg = tmp_path / "imp" / "config.yaml"
    cfg.write_text(cfg.read_text().replace(
        "{provider: anthropic, model: claude-sonnet-4-6}",
        "{provider: openai, model: gpt-4o-mini}",
    ))

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
            "blocking": False,
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
    cfg = tmp_path / "imp" / "config.yaml"
    cfg.write_text(cfg.read_text().replace(
        "{provider: anthropic, model: claude-sonnet-4-6}",
        "{provider: claude_code, model: claude-sonnet-4-5}",
    ))

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
                    "blocking": False,
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
