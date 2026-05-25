import dataclasses

import pytest

from imp.engine.types import (
    Action,
    AgentDef,
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


def test_action_kinds_are_distinct_types() -> None:
    assert isinstance(Continue(), Action)
    assert isinstance(Retry(), Action)
    assert isinstance(Pause(), Action)
    assert isinstance(Goto(target="spec"), Action)


def test_action_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        Goto(target="x").target = "y"  # type: ignore[misc]


def test_workflow_frozen() -> None:
    wf = Workflow(
        name="t",
        description="",
        phases=(),
        defaults=WorkflowDefaults(max_retries=3, on_max_retries=Pause()),
        agents={},
        workflow_dir=__import__("pathlib").Path("."),
        source=SourceLoc(file=__import__("pathlib").Path("workflow.yaml"), line=1),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        wf.name = "x"  # type: ignore[misc]


def test_source_loc_format() -> None:
    loc = SourceLoc(file=__import__("pathlib").Path("agents/x.md"), line=12)
    assert str(loc).endswith("agents/x.md:12")


def test_output_schema_has_status_helper() -> None:
    schema = OutputSchema(
        fields=(
            OutputField(name="status", type="enum", values=("Success", "Error"), required=True),
        ),
        source=SourceLoc(file=__import__("pathlib").Path("a.md"), line=1),
    )
    assert schema.status_field().name == "status"
    assert "Success" in schema.status_field().values
