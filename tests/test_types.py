# tests/test_types.py
from __future__ import annotations

from pathlib import Path

from imp.engine.expr import BoolLit, Name, StrLit, VarRef
from imp.engine.types import (
    Continue,
    Goto,
    HumanOption,
    Pause,
    Phase,
    Rule,
    SourceLoc,
    Step,
    VarDecl,
    Vars,
    Workflow,
    WorkflowDefaults,
)


def loc(line: int = 1) -> SourceLoc:
    return SourceLoc(file=Path("workflow.yaml"), line=line)


def _agent(name: str, phase: str, **kw) -> Step:
    return Step(name=name, phase=phase, kind="agent", rules=(), options=(),
                on_max_retries=Pause(), max_loops=10, source=loc(),
                file=Path(f"agents/{name}.md"), **kw)


def test_goto_default_feedback_none() -> None:
    assert Goto(target="x").feedback is None
    assert Goto(target="x", feedback="Findings:").feedback == "Findings:"


def test_goto_resume_defaults_false_and_affects_equality() -> None:
    assert Goto(target="x").resume is False
    assert Goto(target="x", resume=True).resume is True
    # resume participates in value equality (frozen dataclass)
    assert Goto(target="x") != Goto(target="x", resume=True)


def test_rule_is_else_when_condition_none() -> None:
    r = Rule(condition=None, action=Continue(), mutations=(), source=loc())
    assert r.is_else is True
    r2 = Rule(condition=Name("review_done"), action=Goto("implement"),
              mutations=(("round", BoolLit(True)),), source=loc())
    assert r2.is_else is False


def test_human_option_carries_action_and_mutations() -> None:
    opt = HumanOption(label="Hold", action=Pause(),
                      mutations=(("holds", VarRef("holds")),), source=loc())
    assert opt.label == "Hold"
    assert isinstance(opt.action, Pause)


def test_vars_helpers() -> None:
    v = Vars(decls=(
        VarDecl(name="review_done", type="bool", default=False, source=loc()),
        VarDecl(name="round", type="int", default=0, source=loc()),
    ))
    assert v.types() == {"review_done": "bool", "round": "int"}
    assert v.defaults() == {"review_done": False, "round": 0}
    assert v.names() == frozenset({"review_done", "round"})


def test_workflow_flat_steps_and_goto_resolution() -> None:
    planning = Phase(name="planning", source=loc(), steps=(
        _agent("spec-writer", "planning"),
        Step(name="route", phase="planning", kind="branch",
             rules=(Rule(condition=Name("review_done"), action=Goto("implement"),
                         mutations=(), source=loc()),
                    Rule(condition=None, action=Continue(), mutations=(), source=loc())),
             options=(), on_max_retries=Pause(), max_loops=10, source=loc()),
    ))
    implement = Phase(name="implement", source=loc(), steps=(
        _agent("coder", "implement"),
    ))
    merge = Phase(name="merge", source=loc(), steps=())  # trailing empty
    wf = Workflow(
        name="t", description="", phases=(planning, implement, merge),
        vars=Vars(decls=(VarDecl("review_done", "bool", False, loc()),)),
        defaults=WorkflowDefaults(max_retries=3, on_max_retries=Pause()),
        agents={}, workflow_dir=Path("."), source=loc(),
    )
    assert [s.name for s in wf.flat_steps()] == ["spec-writer", "route", "coder"]
    assert wf.resolve_goto("coder") == 2            # step name
    assert wf.resolve_goto("implement") == 2        # phase -> its first step
    assert wf.resolve_goto("spec-writer") == 0
    # empty trailing phase -> len(flat) sentinel (terminal)
    assert wf.resolve_goto("merge") == 3


def test_workflow_defaults_max_loops_default() -> None:
    d = WorkflowDefaults(max_retries=3, on_max_retries=Pause())
    assert d.max_loops == 10


def test_role_constraint_defaults_and_fields():
    from imp.engine.types import RoleConstraint
    c = RoleConstraint(differ_provider_from="code")
    assert c.differ_provider_from == "code"
    assert c.source is None
    assert RoleConstraint().differ_provider_from is None


def test_agentdef_model_role_defaults_to_none():
    from pathlib import Path

    from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
    a = AgentDef(
        name="x", description="", tools=frozenset(), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error")),),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1))
    assert a.model_role is None  # CRITICAL: None, not "default"


def test_workflow_model_rules_defaults_to_empty():
    wf = Workflow(
        name="w", description="", phases=(), vars=Vars(decls=()),
        defaults=WorkflowDefaults(max_retries=0, on_max_retries=Pause()),
        agents={}, workflow_dir=Path("."), source=SourceLoc(file=Path("w"), line=1))
    assert wf.model_rules == {}
