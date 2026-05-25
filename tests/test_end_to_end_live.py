"""Live Anthropic end-to-end. Gated by IMP_LIVE_API=1. Costs a small amount
of Anthropic credit per run."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from imp.agents.deepagent_runner import DeepAgentRunner
from imp.engine.change import Change
from imp.engine.executor import RunResult, run

FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = pytest.mark.skipif(
    os.environ.get("IMP_LIVE_API") != "1",
    reason="set IMP_LIVE_API=1 + ANTHROPIC_API_KEY to enable",
)


def test_one_step_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The autouse conftest fixture (Task 8) leaves real ANTHROPIC_API_KEY
    # alone if already set. If we're here with the live flag, expect a
    # non-stub key.
    real_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if real_key == "sk-test":
        pytest.skip("autouse fixture is masking real ANTHROPIC_API_KEY "
                    "(set the env var before running)")
    if not real_key:
        pytest.skip("ANTHROPIC_API_KEY not set")

    shutil.copytree(
        FIXTURES / "workflows" / "live-e2e",
        tmp_path / ".imp" / "workflows" / "default",
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: [{provider: anthropic, "
        "model: claude-haiku-4-5-20251001}]}\n"
    )
    monkeypatch.chdir(tmp_path)

    change = Change.create(tmp_path, title="live")
    result = run(change, DeepAgentRunner())
    assert result is RunResult.COMPLETED

    events = list(change.event_log().read())
    outputs = [e for e in events if e["type"] == "agent_output"]
    assert len(outputs) == 1
    assert outputs[0]["payload"]["status"] == "Success"
    # The model usually says "hello world" verbatim per the system prompt;
    # be lenient in case it doesn't quite.
    assert "hello" in outputs[0]["payload"].get("message", "").lower()
