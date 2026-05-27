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
