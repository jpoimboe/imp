"""End-to-end test of the shipped default workflow via mocked Anthropic.

Plan 3 reconnects the full workflow.yaml (with agent gates) after Plan 2
landed all three runners. The shipped script gates check that artifact files
exist; we replace them with `exit 0` shims because the fake provider doesn't
write real artifacts.

What this proves:
- Multi-phase progression (onboard → spec → design → plan → implement → review → merge)
- AgentGate retry/continue semantics through the executor
- Human gate option-1 path through every phase
- All shipped agent .md files parse and run via the executor
- Step-level script-gate evaluation chains correctly with phase-level agent
  gates
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from imp.providers import resolve
from imp.providers.anthropic import AnthropicProvider
from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _seed_default_workflow(tmp_path: Path) -> Change:
    """Copy the shipped default workflow into the change root, then replace
    the script-gate shells with `exit 0` stubs."""
    src = Path(__file__).resolve().parents[1] / "src" / "imp" / "assets" / "workflows" / "default"
    dst = tmp_path / ".imp" / "workflows" / "default"
    shutil.copytree(src, dst)
    # Replace script gates with always-pass stubs.
    for shell in (dst / "scripts").glob("*.sh"):
        shell.write_text("#!/bin/sh\nexit 0\n")
        shell.chmod(0o755)

    # config.yaml: a single role for every model_role used by the workflow.
    cfg = tmp_path / ".imp" / "config.yaml"
    cfg.write_text(
        "roles:\n"
        "  default:       {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  spec:          {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  spec_review:   {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  design:        {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  design_review: {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  code:          {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  code_review:   {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
    )
    return Change.create(tmp_path, title="e2e-default-workflow")


@pytest.mark.anyio
async def test_full_default_workflow_completes_with_canned_anthropic(
    tmp_path: Path,
) -> None:
    change = _seed_default_workflow(tmp_path)

    # Default response: complete_step with all-fields-success. The fake's
    # default_response is consulted whenever the canned list is empty.
    #
    # LOAD-BEARING: this strategy relies on `validate_agent_output` (in
    # src/imp/engine/schema.py:72) iterating `schema.fields` only — extra
    # keys in the output payload are silently ignored, not rejected. If a
    # future tightening makes the validator strict, this test breaks and
    # the fix is to construct per-agent canned responses instead.
    default = response(
        tool_use("c0", "complete_step", {
            "status": "Success",
            "verdict": "approve",
            "summary": "ok",
            "notes": "looks good",
            "spec": "stub spec body",
            "design": "stub design body",
            "plan": "stub plan body",
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

    # Auto-pick option 1 for every human gate (onboard continue, spec approve,
    # design approve, plan approve, review merge, merge close).
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
    # 6 human gates: onboard, spec, design, plan, review, merge.
    assert answer_count[0] == 6


@pytest.mark.anyio
async def test_full_default_workflow_completes_with_canned_openai(
    tmp_path: Path,
) -> None:
    from imp.providers.openai import OpenAIProvider
    from tests.openai_fakes import FakeAsyncOpenAI, completion, message, tool_call

    change = _seed_default_workflow(tmp_path)
    # Rewrite config to OpenAI provider for every role.
    cfg = tmp_path / ".imp" / "config.yaml"
    cfg.write_text(cfg.read_text().replace(
        "{provider: anthropic, model: claude-sonnet-4-6}",
        "{provider: openai, model: gpt-4o-mini}",
    ))

    default_completion = completion(message(
        content=None,
        tool_calls=[tool_call("c0", "complete_step", {
            "status": "Success", "verdict": "approve",
            "summary": "ok", "notes": "ok",
            "spec": "stub", "design": "stub", "plan": "stub",
        })],
    ))
    fake = FakeAsyncOpenAI([], default_response=default_completion)

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
    cfg = tmp_path / ".imp" / "config.yaml"
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
                    "status": "Success", "verdict": "approve",
                    "summary": "ok", "notes": "ok",
                    "spec": "stub", "design": "stub", "plan": "stub",
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
