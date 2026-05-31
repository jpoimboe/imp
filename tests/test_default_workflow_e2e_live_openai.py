"""Live end-to-end test of the OpenAI runner against the real API.

Skips without OPENAI_API_KEY env var or ~/.openai-key file. Costs ~1 cent
per run. Tests the whole stack: provider resolution → message translation →
API call → tool dispatch → complete_step → agent_output validation.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change
from tests._live import opted_in


def _openai_key() -> str | None:
    """Resolve OpenAI API key from env var or `~/.openai-key` file."""
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    key_file = Path.home() / ".openai-key"
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
    not opted_in("openai"),
    reason="openai not in IMP_LIVE_PROVIDERS",
)
@pytest.mark.anyio
async def test_one_step_walk_against_openai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _openai_key()
    assert key is not None, (
        "openai opted in via IMP_LIVE_PROVIDERS but no "
        "OPENAI_API_KEY env var or ~/.openai-key file found"
    )
    monkeypatch.setenv("OPENAI_API_KEY", key)
    wf = tmp_path / ".imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "---\n"
        "Call complete_step with status=Success. Do not call any other tool.\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: live-openai\ndescription: One-step test\n"
        "defaults: {max_retries: 1, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n"
        "  - name: a\n"
        "    steps:\n"
        "      - {name: w, file: agents/writer.md, model_role: default}\n"
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default:\n    prefer: [{provider: openai, model: gpt-4o-mini}]\n"
    )
    change = Change.create(tmp_path, title="live-openai")

    result = await run_change(change)
    assert result is RunResult.COMPLETED
