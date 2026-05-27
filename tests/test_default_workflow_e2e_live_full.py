"""Full shipped default workflow end-to-end against the real Anthropic API.

This is the *expensive* live test: 8+ Sonnet calls per run, ~10-30¢ per
invocation. Gated behind TWO opt-ins so accidental CI execution doesn't burn
budget:
  1. ANTHROPIC_API_KEY (or ~/.anthropic-key) must be present.
  2. IMP_RUN_LIVE_TESTS=1 must be set.

Cheap one-step live tests (e2e_live, e2e_live_openai, etc.) keep their
single-gate skip — they're inexpensive enough to run on every push when
the key is set."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from imp.engine.change import Change
from imp.engine.executor import RunResult, run_change


def _anthropic_key() -> str | None:
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


pytestmark = [
    pytest.mark.skipif(
        not _anthropic_key(),
        reason="full live walk requires ANTHROPIC_API_KEY or ~/.anthropic-key",
    ),
    pytest.mark.skipif(
        os.environ.get("IMP_RUN_LIVE_TESTS") != "1",
        reason="expensive (~10-30¢); set IMP_RUN_LIVE_TESTS=1 to run",
    ),
]


@pytest.mark.anyio
async def test_full_default_workflow_real_anthropic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _anthropic_key()
    assert key is not None
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)

    src = Path(__file__).resolve().parents[1] / "src" / "imp" / "assets" / "workflows" / "default"
    dst = tmp_path / ".imp" / "workflows" / "default"
    shutil.copytree(src, dst)
    for shell in (dst / "scripts").glob("*.sh"):
        shell.write_text("#!/bin/sh\nexit 0\n")
        shell.chmod(0o755)

    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n"
        "  default:       {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  spec:          {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  spec_review:   {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  design:        {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  design_review: {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  code:          {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
        "  code_review:   {prefer: [{provider: anthropic, model: claude-sonnet-4-6}]}\n"
    )

    # Pre-write a minimal /artifacts/intent.md so context-loader has something
    # to reason about. The default workflow assumes intent-clarifier feeds the
    # spec writer; without intent-clarifier in V1's shipped workflow.yaml the
    # spec writer just reasons from change.title.
    change = Change.create(
        tmp_path, title="Add a trivial sentinel printout to the bash tool",
    )

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
    # Either COMPLETED or PAUSED (if a reviewer rejected enough times to hit
    # max_retries). Both are acceptable; what's NOT acceptable is ERRORED.
    assert result in (RunResult.COMPLETED, RunResult.PAUSED), (
        f"unexpected {result!r}"
    )
