from __future__ import annotations

from pathlib import Path

from imp.assets import bundled_workflow_dir, bundled_workflow_names
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Continue, Goto, Pause

FLAT = Path("src/imp/assets/workflows/imp-flat")
AGENTS = FLAT / "agents"


def _flat():
    return load_workflow(bundled_workflow_dir("imp-flat"))


def _steps(wf):
    return {s.name: s for s in wf.flat_steps()}


# All declared imp-flat vars at their defaults — a realistic evaluate_rules fixture.
_FLAT_VARS = {
    "fast_path": False,
    "spec_minors_integrated": False,
    "plan_minors_integrated": False,
    "review_scope": "plan",
    "review_base": "",
    "coverage_settled": False,
    "integration_fork": "",
    "plan_index": 1,
    "plan_total": 1,
    "task_total": 0,
}


def test_imp_flat_is_bundled_and_loads() -> None:
    assert "imp-flat" in bundled_workflow_names()
    wf = _flat()
    assert wf.name == "feature"
    assert [p.name for p in wf.phases] == [
        "front-door", "spec", "plan", "implement", "integrate"]
