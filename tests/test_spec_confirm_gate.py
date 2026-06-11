"""Rule-path unit tests for the spec confirm gate (design §Feature 1).

Every test loads the SHIPPED default workflow and drives its real rules through
evaluate_rules — no hand-built rule fixtures — so a yaml edit that breaks a rule
path fails here.
"""
from __future__ import annotations

from imp.assets import default_workflow_dir
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Continue, Goto


def _wf():
    return load_workflow(default_workflow_dir())


def _steps(wf):
    return {s.name: s for s in wf.flat_steps()}


# All declared workflow vars at their defaults — a realistic evaluate_rules env.
_BASE_VARS = {
    "ready_to_merge": False, "final_review": True, "fast_path": False,
    "spec_minors_integrated": False, "plan_minors_integrated": False,
    "panel_done": False, "integration_needs_review": False,
    "spec_confirm": True, "spec_unseen_changes": False,
    "spec_gate_pending": False, "spec_gate_passed": False,
}


def test_new_gate_vars_declared_with_design_defaults() -> None:
    defaults = _wf().vars.defaults()
    assert defaults["spec_confirm"] is True
    assert defaults["spec_unseen_changes"] is False
    assert defaults["spec_gate_pending"] is False
    assert defaults["spec_gate_passed"] is False
