from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation
from imp.agents.stub_runner import StubAgentRunner
from imp.engine.loader import parse_agent_file
from tests.conftest import write_agent_md

FIXTURES = Path(__file__).parent / "fixtures"


def _make_invocation(tmp_path: Path, name: str) -> AgentInvocation:
    md = write_agent_md(
        tmp_path / f"{name}.md",
        frontmatter=(
            f"name: {name}\ntools: [complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    agent = parse_agent_file(md)
    return AgentInvocation(
        agent_def=agent,
        model_role="default",
        change_id=1,
        change_artifacts_dir=tmp_path,
        project_root=tmp_path,
        interactive=False,
    )


def test_stub_runner_yields_success(tmp_path: Path) -> None:
    runner = StubAgentRunner(canned_path=FIXTURES / "canned" / "stub_outputs.yaml")
    inv = _make_invocation(tmp_path, "stub")
    events = list(runner.run(inv))
    types = [e["type"] for e in events]
    assert types == ["agent_message", "agent_output"]
    assert events[-1]["payload"] == {"status": "Success"}


def test_stub_runner_yields_error(tmp_path: Path) -> None:
    runner = StubAgentRunner(canned_path=FIXTURES / "canned" / "stub_outputs.yaml")
    inv = _make_invocation(tmp_path, "failing-stub")
    events = list(runner.run(inv))
    assert events[-1]["type"] == "agent_output"
    assert events[-1]["payload"]["status"] == "Error"


def test_stub_runner_crashes(tmp_path: Path) -> None:
    runner = StubAgentRunner(canned_path=FIXTURES / "canned" / "stub_outputs.yaml")
    inv = _make_invocation(tmp_path, "crashing-stub")
    events = list(runner.run(inv))
    assert events[-1]["type"] == "error"


def test_stub_runner_unknown_agent_raises(tmp_path: Path) -> None:
    runner = StubAgentRunner(canned_path=FIXTURES / "canned" / "stub_outputs.yaml")
    inv = _make_invocation(tmp_path, "no-such-agent")
    with pytest.raises(KeyError):
        list(runner.run(inv))
