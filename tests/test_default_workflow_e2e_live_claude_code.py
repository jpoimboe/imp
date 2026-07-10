"""Live end-to-end test of the Claude Code runner against the real `claude` CLI.

Skips without `claude` on PATH. Costs whatever a one-prompt `claude -p`
costs on your account (subscription, Bedrock, Vertex, etc.).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from imp.engine.quest import Quest
from imp.engine.executor import RunResult, run_change
from tests._live import opted_in
from tests.conftest import _git


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _ensure_builtin_providers() -> None:
    # The provider registry is import-time global state; other tests clear it
    # via reset_providers_for_tests() teardown. Restore the built-ins so this
    # live test is independent of pytest-randomly ordering.
    from imp.providers import register_builtin_providers
    register_builtin_providers()


@pytest.mark.skipif(
    not opted_in("claude_code"),
    reason="claude_code not in IMP_LIVE_PROVIDERS",
)
@pytest.mark.anyio
async def test_one_step_walk_against_claude_code(tmp_path: Path) -> None:
    assert shutil.which("claude") is not None, (
        "claude_code opted in via IMP_LIVE_PROVIDERS but `claude` CLI "
        "not found on PATH"
    )
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / ".gitignore").write_text(".imp/\n")
    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\n"
        "Call complete_step with status=Success. Do not call any other tool.\n"
    )
    # New inline-surface format: phases is a list of {name, steps}.
    # on_max_retries uses verb-style: {pause: true}.
    (wf / "workflow.yaml").write_text(
        "name: live-cc\ndescription: One-step test\n"
        "defaults: {max_retries: 1, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n"
        "  - name: a\n"
        "    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    (tmp_path / "imp" / "project.yaml").write_text(
        "default-workflow: imp\n"
        "providers: [claude_code]\n"
        "tiers:\n"
        "  low:    {claude_code: claude-haiku-4-5-20251001}\n"
        "  medium: {claude_code: claude-haiku-4-5-20251001}\n"
        "  high:   {claude_code: claude-haiku-4-5-20251001}\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    change = Quest.create(
        tmp_path, title="live-cc", quest_id="live-cc", base_branch="main")

    result = await run_change(change)
    assert result is RunResult.COMPLETED
