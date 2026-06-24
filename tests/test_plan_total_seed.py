from __future__ import annotations

from pathlib import Path

from imp.assets import bundled_workflow_dir
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Continue

AGENTS_DIR = Path("src/imp/assets/workflows/imp/agents")


def _wf():
    return load_workflow(bundled_workflow_dir("imp"))


def _plan_writer_step():
    return {s.name: s for s in _wf().flat_steps()}["plan-writer"]


def test_plan_writer_schema_declares_required_plan_total() -> None:
    fields = {f.name: f for f in _wf().agents["plan-writer"].output_schema.fields}
    assert "plan_total" in fields
    assert fields["plan_total"].type == "int"
    assert fields["plan_total"].required is True


def test_plan_writer_seeds_plan_total_on_the_first_plan() -> None:
    m = evaluate_rules(_plan_writer_step().rules,
                       vars={"plan_index": 1}, output={"plan_total": 3})
    assert m.var_mutations == {"plan_total": 3}
    assert isinstance(m.action, Continue)


def test_plan_writer_does_not_reseed_plan_total_on_later_plans() -> None:
    m = evaluate_rules(_plan_writer_step().rules,
                       vars={"plan_index": 2}, output={"plan_total": 99})
    assert "plan_total" not in m.var_mutations
    assert isinstance(m.action, Continue)
