from __future__ import annotations

from pathlib import Path

from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow, parse_agent_file

AGENTS = Path("src/imp/assets/workflows/imp/agents")
SWEEP = AGENTS / "deferred-sweep.md"


def test_deferred_sweep_parses_with_expected_contract() -> None:
    agent = parse_agent_file(SWEEP)
    assert agent.name == "deferred-sweep"
    assert set(agent.subagents) == {"implementer", "quality-reviewer"}
    fields = agent.output_schema.field_names()
    assert "status" in fields and "summary" in fields


def test_deferred_sweep_resolves_to_dispatcher_tools() -> None:
    wf = load_workflow(bundled_workflow_dir("imp"))
    agent = wf.agents["deferred-sweep"]
    for tool in ("read_file", "write_file", "edit_file", "bash", "task"):
        assert tool in agent.tools, f"missing tool {tool}"
    assert "complete_step" not in agent.tools


def test_deferred_sweep_loads_as_part_of_the_workflow() -> None:
    wf = load_workflow(bundled_workflow_dir("imp"),
                       configured_tiers=frozenset({"low", "medium", "high"}))
    assert "deferred-sweep" in wf.agents


def test_deferred_sweep_prompt_pins_three_dispositions_and_safeguards() -> None:
    low = SWEEP.read_text().lower()
    # The three dispositions of the new risk-gated model.
    assert "record it `fixed`" in low and "needs-decision" in low and "dismissed" in low
    # Risk, not scope, is the gate.
    assert "low-risk" in low and "high-risk" in low
    # The pinned input line the sweep parses.
    assert "- [ ] deferred" in low
    # Reviewer discipline: trivial/mechanical fixes skip the quality-reviewer.
    assert "no quality-reviewer" in low or "without a quality-reviewer" in low
    # Non-trivial fixes must dispatch the quality-reviewer (other half of AC 2).
    assert "non-trivial" in low and "quality-reviewer" in low
    # Commit granularity preserved.
    assert "one clean commit" in low
    # The scope-ceiling phrasing is gone (AC 1).
    assert "never grow the change" not in low
    # Regression guard: plan-writer routing vocabulary must not reappear.
    assert "auto-route" not in low
    # Polish never marks the run degraded; fail-safe retained.
    assert "degraded" in low
    assert "malformed" in low
    # The three output formats that finalize reads must be pinned.
    assert "— fixed" in low and "— needs-decision:" in low and "— dismissed:" in low
