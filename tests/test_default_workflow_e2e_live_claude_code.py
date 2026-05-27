"""Live end-to-end test of the Claude Code runner against the real `claude` CLI.

Skips without `claude` on PATH. Costs whatever a one-prompt `claude -p`
costs on your account (subscription, Bedrock, Vertex, etc.).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="`claude` CLI not on PATH; skip live Claude Code walk",
)
@pytest.mark.anyio
async def test_one_step_walk_against_claude_code(tmp_path: Path) -> None:
    wf = tmp_path / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\n"
        "Call complete_step with status=Success. Do not call any other tool.\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: live-cc\ndescription: One-step test\nphases: [a]\n"
        "defaults: {max_retries: 1, on_max_retries: {kind: pause}}\n"
        "a:\n  steps:\n    - {name: w, file: agents/writer.md, model_role: default}\n"
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer: [{provider: claude_code, model: claude-sonnet-4-5}]\n"
    )
    change = Change.create(tmp_path, title="live-cc")

    result = await run_change(change)
    assert result is RunResult.COMPLETED
