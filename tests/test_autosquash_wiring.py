"""The implement phase routes the production->consumption boundary through
cleanup-implement (the cleanup.sh wrapper). A resolvable fold conflict (exit 2)
falls through into autosquash-resolve-implement, which loops back to
cleanup-implement in verify-mode."""
from __future__ import annotations

from pathlib import Path

from imp.engine.loader import load_workflow
from imp.engine.types import Continue, Goto, Pause

WF_DIR = Path("src/imp/assets/workflows/imp")


def _wf():
    return load_workflow(WF_DIR)


def _steps(wf):
    return {s.name: s for s in wf.flat_steps()}


def test_autosquash_step_is_gone() -> None:
    names = [s.name for s in _wf().flat_steps()]
    assert "autosquash" not in names
    assert "review-base-init" not in names
    assert "commit-check" not in names


def test_cleanup_implement_routes_by_exit_code() -> None:
    step = _steps(_wf())["cleanup-implement"]
    assert step.kind == "script"
    assert step.command == './scripts/cleanup.sh "{{ vars.review_base }}"'
    from imp.engine.gates import evaluate_rules
    r = step.rules
    # forward plan (coverage not yet latched): exit 0 SKIPS the reviewer -> deferred-sweep
    fwd = evaluate_rules(r, vars={"coverage_settled": False},
                         output={"exit": 0, "stdout": "", "stderr": ""})
    assert isinstance(fwd.action, Goto) and fwd.action.target == "deferred-sweep"
    # post-settlement corrective (coverage latched): exit 0 -> immediate per-plan review
    corr = evaluate_rules(r, vars={"coverage_settled": True},
                          output={"exit": 0, "stdout": "", "stderr": ""})
    assert isinstance(corr.action, Goto) and corr.action.target == "plan-code-reviewer"
    m2 = evaluate_rules(r, vars={"coverage_settled": False},
                        output={"exit": 2, "stdout": "", "stderr": ""})
    assert isinstance(m2.action, Continue)            # fall through into the resolver
    m3 = evaluate_rules(r, vars={"coverage_settled": False},
                        output={"exit": 3, "stdout": "", "stderr": ""})
    assert isinstance(m3.action, Goto) and m3.action.target == "task-lead"
    assert m3.action.resume is True
    m1 = evaluate_rules(r, vars={"coverage_settled": False},
                        output={"exit": 1, "stdout": "", "stderr": ""})
    assert isinstance(m1.action, Pause)


def test_autosquash_resolve_implement_loops_back_to_cleanup() -> None:
    step = _steps(_wf())["autosquash-resolve-implement"]
    assert step.kind == "agent" and step.tier == "high"
    assert step.file is not None and step.file.name == "autosquash-resolve.md"
    assert isinstance(step.rules[-1].action, Goto)
    assert step.rules[-1].action.target == "cleanup-implement"
    assert step.max_loops == 6


def test_cleanup_implement_precedes_its_resolver_and_reviewer() -> None:
    names = [s.name for s in _wf().flat_steps()]
    assert names.index("cleanup-implement") < names.index("autosquash-resolve-implement")
    assert names.index("autosquash-resolve-implement") < names.index("plan-code-reviewer")
