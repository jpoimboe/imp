from __future__ import annotations

from pathlib import Path

from imp.assets import default_workflow_dir
from imp.engine.loader import load_workflow, parse_agent_file

AGENTS = Path("src/imp/assets/workflows/default/agents")
SWEEP = AGENTS / "deferred-sweep.md"


def test_deferred_sweep_parses_with_expected_contract() -> None:
    agent = parse_agent_file(SWEEP)
    assert agent.name == "deferred-sweep"
    assert set(agent.subagents) == {"implementer", "quality-reviewer"}
    for tool in ("complete_step", "read_file", "write_file", "edit_file",
                 "bash", "task"):
        assert tool in agent.tools, f"missing tool {tool}"
    fields = agent.output_schema.field_names()
    assert "status" in fields and "summary" in fields


def test_deferred_sweep_loads_as_part_of_the_workflow() -> None:
    wf = load_workflow(default_workflow_dir(),
                       configured_tiers=frozenset({"low", "medium", "high"}))
    assert "deferred-sweep" in wf.agents


def test_deferred_sweep_prompt_pins_three_dispositions_and_safeguards() -> None:
    low = SWEEP.read_text().lower()
    assert "silent" in low and "fyi" in low and "veto" in low
    assert "surface-for-decision" in low or "surface for decision" in low
    assert "- [ ] deferred" in low
    assert "silent-fixed" in low and "fyi-fixed" in low and "still-deferred" in low
    assert "no quality-reviewer" in low or "without a quality-reviewer" in low
    assert "one clean commit" in low
    assert "plan-writer" in low and "never" in low
    assert "degraded" in low
    assert "malformed" in low
    assert "fixability bar" in low and "scope" in low
