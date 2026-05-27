"""Live end-to-end test of the OpenAI runner against the real API.

Skips without OPENAI_API_KEY. Costs ~1 cent per run. Tests the whole stack:
provider resolution → message translation → API call → tool dispatch →
complete_step → agent_output validation.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set; skip live OpenAI walk",
)
@pytest.mark.anyio
async def test_one_step_walk_against_openai(tmp_path: Path) -> None:
    wf = tmp_path / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\n"
        "Call complete_step with status=Success. Do not call any other tool.\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: live-openai\ndescription: One-step test\nphases: [a]\n"
        "defaults: {max_retries: 1, on_max_retries: {kind: pause}}\n"
        "a:\n  steps:\n    - {name: w, file: agents/writer.md, model_role: default}\n"
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer: [{provider: openai, model: gpt-4o-mini}]\n"
    )
    change = Change.create(tmp_path, title="live-openai")

    result = await run_change(change)
    assert result is RunResult.COMPLETED
