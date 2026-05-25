import dataclasses
from pathlib import Path

import pytest

from imp.engine.types import (
    Action,
    AgentDef,
    AgentGate,
    Continue,
    Decide,
    FsRule,
    Gate,
    Goto,
    HumanGate,
    HumanOption,
    OutputField,
    OutputSchema,
    Pause,
    Phase,
    Retry,
    Rule,
    ScriptGate,
    SourceLoc,
    Step,
    StubGate,
    Workflow,
    WorkflowDefaults,
)

# ── small shared fixture ───────────────────────────────────────────────────

_LOC = SourceLoc(file=Path("x.yaml"), line=1)


def _output_schema() -> OutputSchema:
    return OutputSchema(
        fields=(
            OutputField(name="status", type="enum",
                        values=("Success", "Error"), required=True),
        ),
        source=_LOC,
    )


def _step(name: str = "s") -> Step:
    return Step(
        name=name,
        file=Path("agents/stub.md"),
        interactive=False,
        model_role="default",
        fs_permissions=(),
        gates=(),
        max_retries=3,
        on_max_retries=Pause(),
        source=_LOC,
    )


# ── action types ──────────────────────────────────────────────────────────

def test_action_kinds_are_distinct_types() -> None:
    assert isinstance(Continue(), Action)
    assert isinstance(Retry(), Action)
    assert isinstance(Pause(), Action)
    assert isinstance(Goto(target="spec"), Action)


def test_action_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        Goto(target="x").target = "y"  # type: ignore[misc]


# ── rule invariant ────────────────────────────────────────────────────────

def test_rule_else_catches_all() -> None:
    r = Rule(when=(), is_else=True, action=Continue(), source=_LOC)
    assert r.is_else


def test_rule_with_when_matches() -> None:
    r = Rule(when=(("exit", 0),), is_else=False,
             action=Continue(), source=_LOC)
    assert not r.is_else


def test_rule_rejects_both_when_and_else() -> None:
    with pytest.raises(ValueError, match="empty when"):
        Rule(when=(("exit", 0),), is_else=True,
             action=Continue(), source=_LOC)


def test_rule_rejects_empty_non_else() -> None:
    with pytest.raises(ValueError, match="black hole"):
        Rule(when=(), is_else=False,
             action=Continue(), source=_LOC)


def test_decide_constructs() -> None:
    d = Decide(
        rules=(
            Rule(when=(("exit", 0),), is_else=False,
                 action=Continue(), source=_LOC),
            Rule(when=(), is_else=True, action=Retry(), source=_LOC),
        ),
        source=_LOC,
    )
    assert len(d.rules) == 2


# ── source location format ────────────────────────────────────────────────

def test_source_loc_format() -> None:
    loc = SourceLoc(file=Path("agents/x.md"), line=12)
    assert str(loc) == "agents/x.md:12"


# ── output schema ─────────────────────────────────────────────────────────

def test_output_schema_status_helper() -> None:
    schema = _output_schema()
    sf = schema.status_field()
    assert sf is not None
    assert sf.name == "status"
    assert "Success" in sf.values


def test_output_schema_field_names() -> None:
    schema = OutputSchema(
        fields=(
            OutputField(name="status", type="enum",
                        values=("Success", "Error"), required=True),
            OutputField(name="summary", type="string", required=False),
        ),
        source=_LOC,
    )
    assert schema.field_names() == frozenset({"status", "summary"})


# ── filesystem rules ──────────────────────────────────────────────────────

def test_fs_rule_constructs() -> None:
    rule = FsRule(
        operations=("read",),
        paths=("/project/**",),
        mode="allow",
        source=_LOC,
    )
    assert rule.mode == "allow"
    assert rule.operations == ("read",)


# ── gates (frozen+slots inheritance) ──────────────────────────────────────

def test_human_gate_constructs_and_subclasses_gate() -> None:
    gate = HumanGate(
        prompt="Continue?",
        options=(
            HumanOption(label="Yes", action=Continue(), source=_LOC),
        ),
        source=_LOC,
    )
    assert isinstance(gate, Gate)
    assert gate.prompt == "Continue?"


def test_script_gate_constructs() -> None:
    decide = Decide(
        rules=(Rule(when=(), is_else=True,
                    action=Continue(), source=_LOC),),
        source=_LOC,
    )
    gate = ScriptGate(
        command="true",
        cwd=None,
        timeout_seconds=None,
        decide=decide,
        source=_LOC,
    )
    assert isinstance(gate, Gate)


def test_agent_gate_constructs() -> None:
    decide = Decide(
        rules=(Rule(when=(), is_else=True,
                    action=Continue(), source=_LOC),),
        source=_LOC,
    )
    gate = AgentGate(
        file=Path("agents/reviewer.md"),
        model_role="default",
        fs_permissions=(),
        decide=decide,
        source=_LOC,
    )
    assert isinstance(gate, Gate)


def test_stub_gate_constructs() -> None:
    gate = StubGate(action=Continue(), source=_LOC)
    assert isinstance(gate, Gate)


# ── steps and phases ──────────────────────────────────────────────────────

def test_step_constructs() -> None:
    step = _step()
    assert step.name == "s"
    assert step.gates == ()


def test_phase_constructs() -> None:
    phase = Phase(
        name="onboard",
        steps=(_step("s1"), _step("s2")),
        gates=(),
        max_retries=3,
        on_max_retries=Pause(),
        source=_LOC,
    )
    assert len(phase.steps) == 2


# ── agent definition ──────────────────────────────────────────────────────

def test_agent_def_constructs() -> None:
    a = AgentDef(
        name="stub",
        description="",
        tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=_output_schema(),
        prompt_template_source="body",
        source_path=Path("agents/stub.md"),
        source=_LOC,
    )
    assert a.name == "stub"
    assert "complete_step" in a.tools


# ── workflow & helpers ────────────────────────────────────────────────────

def _make_workflow(phase_names: tuple[str, ...]) -> Workflow:
    phases = tuple(
        Phase(name=n, steps=(_step(),), gates=(),
              max_retries=3, on_max_retries=Pause(), source=_LOC)
        for n in phase_names
    )
    return Workflow(
        name="t",
        description="",
        phases=phases,
        defaults=WorkflowDefaults(max_retries=3, on_max_retries=Pause()),
        agents={},
        workflow_dir=Path("."),
        source=_LOC,
    )


def test_workflow_frozen() -> None:
    wf = _make_workflow(("p",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        wf.name = "x"  # type: ignore[misc]


def test_workflow_phase_index_finds_middle() -> None:
    wf = _make_workflow(("a", "b", "c"))
    assert wf.phase_index("a") == 0
    assert wf.phase_index("b") == 1
    assert wf.phase_index("c") == 2


def test_workflow_phase_index_unknown_returns_none() -> None:
    wf = _make_workflow(("a", "b"))
    assert wf.phase_index("nope") is None


def test_workflow_next_phase() -> None:
    wf = _make_workflow(("a", "b", "c"))
    assert wf.next_phase("a") == "b"
    assert wf.next_phase("b") == "c"


def test_workflow_next_phase_last_returns_none() -> None:
    wf = _make_workflow(("a", "b"))
    assert wf.next_phase("b") is None


def test_workflow_next_phase_unknown_returns_none() -> None:
    wf = _make_workflow(("a", "b"))
    assert wf.next_phase("nope") is None
