"""The default workflow routes a plain-path autosquash fold conflict (exit 2) to the
autosquash-resolve agent, then loops back to autosquash.sh in verify-mode (no separate
autosquash-verify step). The verify-mode bound and the defensive engine backstop are
both in place."""
from __future__ import annotations

from pathlib import Path

from imp.engine.expr import Binary, IntLit, Name
from imp.engine.loader import load_workflow
from imp.engine.types import Continue, Goto, Pause

WF_DIR = Path("src/imp/assets/workflows/imp")


def _wf():
    return load_workflow(WF_DIR)


def test_resolve_loops_back_to_autosquash_no_verify_step() -> None:
    names = [s.name for s in _wf().flat_steps()]
    assert "autosquash-verify" not in names
    assert names.index("autosquash") < names.index("autosquash-resolve")


def test_autosquash_resolve_is_high_tier_agent_that_reruns_autosquash() -> None:
    step = {s.name: s for s in _wf().flat_steps()}["autosquash-resolve"]
    assert step.kind == "agent"
    assert step.tier == "high"
    assert step.file is not None and step.file.name == "autosquash-resolve.md"
    # On finishing, re-run autosquash.sh — re-entry is verify-mode (sentinel present).
    assert isinstance(step.rules[-1].action, Goto)
    assert step.rules[-1].action.target == "autosquash"
    assert step.max_loops == 6  # defensive backstop; the script's 3-attempt bound aborts first


def test_autosquash_exit0_skips_the_resolver() -> None:
    step = {s.name: s for s in _wf().flat_steps()}["autosquash"]
    exit2_rule = next(r for r in step.rules if r.condition == Binary("==", Name("exit"), IntLit(2)))
    exit0_rule = next(r for r in step.rules if r.condition == Binary("==", Name("exit"), IntLit(0)))
    else_rule = next(r for r in step.rules if r.is_else)
    assert isinstance(exit2_rule.action, Continue)  # conflict/re-resolve -> autosquash-resolve
    assert isinstance(exit0_rule.action, Goto)
    assert exit0_rule.action.target == "review"   # clean fold / verified -> skip resolver
    assert isinstance(else_rule.action, Pause)
