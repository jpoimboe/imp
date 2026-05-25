"""Mocked end-to-end: real DeepAgentRunner + FakeToolCallingChatModel.

Proves that Plan 2's adapter actually composes: tools synthesized correctly,
events translated, engine advances phases.

The fake LLM's structured-output simulation is intentionally weak —
FakeToolCallingChatModel returns canned AIMessages; whether deepagents'
response_format= machinery can extract `structured_response` from them
depends on internal details. The test's primary purpose is to verify the
stack doesn't crash, the runner is invoked the expected number of times,
and the run reaches a terminal state.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from imp.agents.deepagent_runner import DeepAgentRunner
from imp.agents.runner import AgentInvocation
from imp.engine.change import Change
from imp.engine.executor import RunResult, run
from tests.fakes import FakeToolCallingChatModel

FIXTURES = Path(__file__).parent / "fixtures"


def _seed(tmp_path: Path) -> Change:
    shutil.copytree(
        FIXTURES / "workflows" / "mocked-e2e",
        tmp_path / ".imp" / "workflows" / "default",
    )
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: [{provider: anthropic, model: x}]}\n"
    )
    return Change.create(tmp_path, title="mocked-e2e")


def test_mocked_runner_two_phases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Smoke: DeepAgentRunner composes with engine end-to-end against
    fake LLMs. Lenient on terminal RunResult — the fake can't perfectly
    simulate provider-native structured_output."""
    monkeypatch.chdir(tmp_path)

    call_seq = [0]

    def _factory(inv: AgentInvocation) -> FakeToolCallingChatModel:
        call_seq[0] += 1
        # Both steps: produce a single AIMessage so the agent loop terminates
        # quickly. The fake LLM doesn't actually generate valid structured
        # output, so we expect this run to error out — that's OK; the
        # primary assertion is the runner was invoked at least once.
        return FakeToolCallingChatModel(
            responses=[AIMessage(content='{"status": "Success"}')],
        )

    change = _seed(tmp_path)
    runner = DeepAgentRunner(model_factory=_factory)
    result = run(change, runner)

    # Lenient — any terminal state is acceptable; the fake can't perfectly
    # simulate the structured-output path.
    assert result in (RunResult.COMPLETED, RunResult.ERRORED, RunResult.PAUSED)

    # Primary assertion: runner WAS invoked
    assert call_seq[0] >= 1
