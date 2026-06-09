# tests/test_gates.py — rewrite
from pathlib import Path

from imp.engine.expr import Binary, BoolLit, Name, StrLit
from imp.engine.gates import evaluate_rules, run_script
from imp.engine.types import Continue, Goto, Pause, Rule, SourceLoc, Step


def loc(line=1): return SourceLoc(file=Path("x"), line=line)


def _rule(cond, action, muts=()):
    return Rule(condition=cond, action=action, mutations=muts, source=loc())


def test_evaluate_rules_first_match_wins() -> None:
    rules = (
        _rule(Binary("==", Name("verdict"), StrLit("rework")), Goto("spec-writer")),
        _rule(Binary("==", Name("verdict"), StrLit("approve")), Goto("spec-writer")),
        _rule(None, Pause()),
    )
    m = evaluate_rules(rules, vars={}, output={"verdict": "rework"})
    assert isinstance(m.action, Goto) and m.action.target == "spec-writer"
    assert m.rule is rules[0]


def test_evaluate_rules_else_fallback() -> None:
    rules = (
        _rule(Binary("==", Name("verdict"), StrLit("approve")), Continue()),
        _rule(None, Pause()),
    )
    m = evaluate_rules(rules, vars={}, output={"verdict": "weird"})
    assert isinstance(m.action, Pause)


def test_evaluate_rules_applies_set_to_returned_mutations() -> None:
    rules = (_rule(None, Continue(), muts=(("review_done", BoolLit(True)),)),)
    m = evaluate_rules(rules, vars={"review_done": False}, output=None)
    assert m.var_mutations == {"review_done": True}


def test_run_script_builds_output_dict(tmp_path) -> None:
    step = Step(name="check", phase="p", kind="script",
                rules=(_rule(None, Continue()),),
                on_max_retries=Pause(), max_loops=10, source=loc(),
                command="echo hi; exit 3")
    out = run_script(step, cwd=tmp_path, env_extra=None)
    assert out["exit"] == 3
    assert "hi" in out["stdout"]


def test_evaluate_rules_empty_returns_continue() -> None:
    """Empty rules block -> continue-on-success default."""
    m = evaluate_rules((), vars={}, output=None)
    assert isinstance(m.action, Continue)
    assert m.rule is None
    assert m.var_mutations == {}


def test_evaluate_rules_no_mutations_when_not_set() -> None:
    """A matched rule with no set: clause returns empty mutations."""
    rules = (_rule(None, Goto("next")),)
    m = evaluate_rules(rules, vars={}, output=None)
    assert isinstance(m.action, Goto)
    assert m.var_mutations == {}
