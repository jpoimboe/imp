from __future__ import annotations

import os
from pathlib import Path

import pytest

from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from tests._live import opted_in


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
    wf = tmp_path / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n  note: {type: string}\n"
        "---\n"
        "You are testing imp. Call complete_step with status=Success and a one-word note.\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\nphases: [only]\n"
        "defaults: {max_retries: 1, on_max_retries: {kind: pause}}\n"
        "only:\n  steps:\n    - {name: w, file: agents/writer.md, model_role: default}\n"
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer: [{provider: anthropic, model: claude-sonnet-4-6}]\n"
    )
    change = Change.create(tmp_path, title="live-smoke")

    result = await run_change(change)
    assert result is RunResult.COMPLETED
