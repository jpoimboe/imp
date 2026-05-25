"""Agent gate: agent runs, output schema-validated, decide block evaluated."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from imp.agents.stub_runner import StubAgentRunner
from imp.engine.change import Change
from imp.engine.executor import RunResult, run

FIXTURES = Path(__file__).parent / "fixtures"


def _seed(tmp_path: Path) -> Change:
    shutil.copytree(
        FIXTURES / "workflows" / "agent-gate",
        tmp_path / ".imp" / "workflows" / "default",
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: [{provider: anthropic, model: x}]}\n"
    )
    return Change.create(tmp_path, title="t")


def test_agent_gate_ok_verdict_continues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "IMP_STUB_CANNED",
        str(FIXTURES / "canned" / "stub_outputs.yaml"),
    )
    monkeypatch.chdir(tmp_path)
    change = _seed(tmp_path)
    runner = StubAgentRunner(canned_path=FIXTURES / "canned" / "stub_outputs.yaml")
    result = run(change, runner)
    assert result is RunResult.COMPLETED
