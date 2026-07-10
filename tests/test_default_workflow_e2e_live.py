from __future__ import annotations

import os
from pathlib import Path

import pytest

from imp.engine.quest import Quest
from imp.engine.executor import RunResult, run_change
from tests._live import opted_in
from tests.conftest import _git


def _anthropic_key() -> str | None:
    """Resolve Anthropic API key from env var or `~/.anthropic-key` file."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    key_file = Path.home() / ".anthropic-key"
    if key_file.exists():
        return key_file.read_text().strip()
    return None


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
    not opted_in("anthropic"),
    reason="anthropic not in IMP_LIVE_PROVIDERS",
)
@pytest.mark.anyio
async def test_live_minimal_change_completes_one_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _anthropic_key()
    assert key is not None, (
        "anthropic opted in via IMP_LIVE_PROVIDERS but no "
        "ANTHROPIC_API_KEY env var or ~/.anthropic-key file found"
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / ".gitignore").write_text(".imp/\n")
    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n  note: {type: string}\n"
        "---\n"
        "You are testing imp. Call complete_step with status=Success and a one-word note.\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    (tmp_path / "imp" / "project.yaml").write_text(
        "default-workflow: imp\n"
        "providers: [anthropic]\n"
        "tiers:\n"
        "  low:    {anthropic: claude-haiku-4-5-20251001}\n"
        "  medium: {anthropic: claude-haiku-4-5-20251001}\n"
        "  high:   {anthropic: claude-haiku-4-5-20251001}\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    change = Quest.create(
        tmp_path, title="live-smoke", quest_id="live-smoke", base_branch="main")

    result = await run_change(change)
    assert result is RunResult.COMPLETED
