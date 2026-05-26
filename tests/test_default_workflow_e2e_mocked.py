from __future__ import annotations

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


def _seed_project(tmp_path: Path) -> Change:
    """Write a slim two-phase workflow (writer step + human gate → doer step).

    The shipped default workflow uses agent gates; Plan 1's _run_gate raises
    NotImplementedError on AgentGate. The slim workflow lets the e2e test
    prove the executor wiring without depending on Plan 3.
    """
    wf = tmp_path / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    for name in ("writer", "doer"):
        (wf / "agents" / f"{name}.md").write_text(
            f"---\nname: {name}\ntools: [complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
            "go {{ change.id }}\n"
        )
    (wf / "workflow.yaml").write_text(
        "name: e2e\ndescription: e2e\n"
        "phases: [a, b]\n"
        "defaults: {max_retries: 1, on_max_retries: {kind: pause}}\n"
        "a:\n"
        "  steps:\n    - {name: w, file: agents/writer.md, model_role: default}\n"
        "  gates:\n"
        "    - type: human\n"
        "      prompt: continue?\n"
        "      options: [{label: Yes, action: {kind: continue}}]\n"
        "b:\n"
        "  steps:\n    - {name: d, file: agents/doer.md, model_role: default}\n"
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer: [{provider: anthropic, model: claude-sonnet-4-6}]\n"
    )
    return Change.create(tmp_path, title="e2e")


@pytest.mark.anyio
async def test_two_phase_workflow_completes_with_canned_complete_step(tmp_path) -> None:
    change = _seed_project(tmp_path)

    # Default response: every LLM call returns a complete_step success.
    default = response(
        tool_use("c0", "complete_step", {"status": "Success"}),
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

    inputs = iter(["1", "1"])  # human gates: pick first option
    result = await run_change(
        change,
        human_input_fn=lambda: next(inputs),
        human_output_fn=lambda _msg: None,
    )
    assert result is RunResult.COMPLETED
