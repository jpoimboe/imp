# tests/test_context_window_1m_live_claude_code.py
"""Live verification that the high tier actually runs with the 1M window.

Skips unless `claude_code` is in IMP_LIVE_PROVIDERS. Costs one real `claude -p`
turn. Reads back the SDK-reported context_window (rawMaxTokens) from the change's
event log and asserts it is 1_000_000 — the empirical confirmation that the 1M
opt-in took effect (spec AC6).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from imp.engine.change import Change
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
async def test_high_tier_run_reports_1m_window(tmp_path: Path) -> None:
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
    (wf / "workflow.yaml").write_text(
        "name: live-cc-1m\ndescription: One high-tier step\n"
        "defaults: {max_retries: 1, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n"
        "  - name: a\n"
        "    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: high}\n"
    )
    (tmp_path / "imp" / "project.yaml").write_text(
        "default-workflow: imp\n"
        "providers: [claude_code]\n"
        "tiers:\n"
        "  low:    {claude_code: haiku}\n"
        "  medium: {claude_code: sonnet}\n"
        "  high:   {claude_code: opus-1m}\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    change = Change.create(
        tmp_path, title="live-cc-1m", change_id="live-cc-1m", base_branch="main")

    result = await run_change(change)
    assert result is RunResult.COMPLETED

    windows = [
        e["payload"]["context_window"]
        for e in change.event_log().read()
        if e["type"] == "llm_response"
        and e["payload"].get("context_window") is not None
    ]
    assert windows, "no llm_response carried a context_window"
    assert max(windows) == 1_000_000, (
        f"high-tier run reported {max(windows)} not 1_000_000 — the beta did "
        f"not enable 1M; switch CLAUDE_CODE['opus-1m'].cli_model to "
        f"'claude-opus-4-8[1m]' in src/imp/providers/catalog.py and re-run."
    )
