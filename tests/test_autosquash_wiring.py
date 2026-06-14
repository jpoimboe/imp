"""The default workflow routes a plain-path autosquash fold conflict (exit 2) to the
autosquash-resolve agent, then autosquash-verify, with the verify bounce and the
defensive engine backstop in place."""
from __future__ import annotations

from pathlib import Path

from imp.engine.expr import Binary, IntLit, Name
from imp.engine.loader import load_workflow
from imp.engine.types import Continue, Goto, Pause

WF_DIR = Path("src/imp/assets/workflows/default")


def _wf():
    return load_workflow(WF_DIR)


def test_resolve_and_verify_steps_sit_between_autosquash_and_stamp() -> None:
    names = [s.name for s in _wf().flat_steps()]
    assert (names.index("autosquash")
            < names.index("autosquash-resolve")
            < names.index("autosquash-verify")
            < names.index("stamp-trailers"))


def test_autosquash_resolve_is_high_tier_agent_with_write() -> None:
    step = {s.name: s for s in _wf().flat_steps()}["autosquash-resolve"]
    assert step.kind == "agent"
    assert step.tier == "high"
    assert step.file is not None and step.file.name == "autosquash-resolve.md"


def test_autosquash_exit0_skips_the_resolver() -> None:
    step = {s.name: s for s in _wf().flat_steps()}["autosquash"]
    exit2_rule = next(r for r in step.rules if r.condition == Binary("==", Name("exit"), IntLit(2)))
    exit0_rule = next(r for r in step.rules if r.condition == Binary("==", Name("exit"), IntLit(0)))
    assert isinstance(exit2_rule.action, Continue)
    assert isinstance(exit0_rule.action, Goto)
    assert exit0_rule.action.target == "stamp-trailers"


def test_autosquash_verify_bounces_to_resolver_with_backstop() -> None:
    step = {s.name: s for s in _wf().flat_steps()}["autosquash-verify"]
    assert step.quiet is True
    assert step.max_loops == 5  # defensive backstop only; the script's own bound aborts first
    exit0_rule = next(r for r in step.rules if r.condition == Binary("==", Name("exit"), IntLit(0)))
    exit2_rule = next(r for r in step.rules if r.condition == Binary("==", Name("exit"), IntLit(2)))
    else_rule = next(r for r in step.rules if r.is_else)
    assert isinstance(exit0_rule.action, Continue)
    assert isinstance(exit2_rule.action, Goto)
    assert exit2_rule.action.target == "autosquash-resolve"
    assert exit2_rule.action.resume is True
    assert isinstance(else_rule.action, Pause)
