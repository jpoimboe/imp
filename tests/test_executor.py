from pathlib import Path

from imp.agents.stub_runner import StubAgentRunner
from imp.engine.change import Change
from imp.engine.executor import execute_phase, execute_step
from imp.engine.loader import load_workflow
from imp.engine.state import State
from imp.engine.types import Continue, Pause, Retry
from tests.conftest import write_agent_md

FIXTURES = Path(__file__).parent / "fixtures"


def _seed(tmp_path: Path, workflow_yaml: str, agents: dict[str, str]) -> Change:
    src = tmp_path / ".imp" / "workflows" / "default"
    (src / "agents").mkdir(parents=True)
    for name, fm in agents.items():
        write_agent_md(src / "agents" / name, frontmatter=fm)
    (src / "workflow.yaml").write_text(workflow_yaml)
    (tmp_path / ".imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: [{provider: anthropic, model: x}]}\n"
    )
    return Change.create(tmp_path, title="t")


_STUB_FM = (
    "name: context-loader\ntools: [complete_step]\n"
    "output:\n  status: {type: enum, values: [Success, Error]}\n"
    "  notes: {type: string, required: false}"
)


# ── Task 19: execute_step ───────────────────────────────────────────────

def test_execute_step_no_gates_continues(tmp_path: Path) -> None:
    ch = _seed(
        tmp_path,
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "p:\n  steps: [{name: context-loader, file: agents/context-loader.md}]\n"
        ),
        agents={"context-loader.md": _STUB_FM},
    )
    wf = load_workflow(ch.workflow_dir)
    step = wf.phases[0].steps[0]
    runner = StubAgentRunner(canned_path=FIXTURES / "canned" / "stub_outputs.yaml")
    state = State.load(ch.state_dir)
    state.step = step.name
    action, output = execute_step(
        step=step,
        agent_def=wf.agents[step.file.stem],
        change=ch,
        runner=runner,
        state=state,
    )
    assert isinstance(action, Continue)
    assert output == {"status": "Success", "notes": "loaded"}
    events = list(ch.event_log().read())
    types = [e["type"] for e in events]
    assert "step_started" in types
    assert "agent_output" in types
    assert "step_completed" in types


def test_execute_step_failing_agent_retries(tmp_path: Path) -> None:
    failing_fm = (
        "name: failing-stub\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}"
    )
    ch = _seed(
        tmp_path,
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "p:\n  steps: [{name: failing-stub, file: agents/failing-stub.md}]\n"
        ),
        agents={"failing-stub.md": failing_fm},
    )
    wf = load_workflow(ch.workflow_dir)
    runner = StubAgentRunner(canned_path=FIXTURES / "canned" / "stub_outputs.yaml")
    state = State.load(ch.state_dir)
    state.step = "failing-stub"
    step = wf.phases[0].steps[0]
    action, _ = execute_step(
        step=step,
        agent_def=wf.agents[step.file.stem],
        change=ch,
        runner=runner,
        state=state,
    )
    assert isinstance(action, Retry)


def test_execute_step_runs_script_gate(tmp_path: Path) -> None:
    ch = _seed(
        tmp_path,
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "p:\n"
            "  steps:\n"
            "    - name: context-loader\n"
            "      file: agents/context-loader.md\n"
            "      gates:\n"
            "        - type: script\n"
            "          command: \"false\"\n"
        ),
        agents={"context-loader.md": _STUB_FM},
    )
    wf = load_workflow(ch.workflow_dir)
    runner = StubAgentRunner(canned_path=FIXTURES / "canned" / "stub_outputs.yaml")
    state = State.load(ch.state_dir)
    state.step = "context-loader"
    step = wf.phases[0].steps[0]
    action, _ = execute_step(
        step=step,
        agent_def=wf.agents[step.file.stem],
        change=ch,
        runner=runner,
        state=state,
    )
    assert isinstance(action, Retry)


# ── Task 20: execute_phase ──────────────────────────────────────────────

def test_phase_runs_all_steps(tmp_path: Path) -> None:
    fm_a = _STUB_FM
    fm_b = (
        "name: intent-clarifier\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "  summary: {type: string, required: false}"
    )
    ch = _seed(
        tmp_path,
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "p:\n  steps:\n"
            "    - {name: context-loader,   file: agents/context-loader.md}\n"
            "    - {name: intent-clarifier, file: agents/intent-clarifier.md}\n"
        ),
        agents={"context-loader.md": fm_a, "intent-clarifier.md": fm_b},
    )
    wf = load_workflow(ch.workflow_dir)
    runner = StubAgentRunner(canned_path=FIXTURES / "canned" / "stub_outputs.yaml")
    state = State.load(ch.state_dir)
    action = execute_phase(
        phase=wf.phases[0], workflow=wf, change=ch, runner=runner, state=state,
    )
    assert isinstance(action, Continue)
    events = list(ch.event_log().read())
    step_names = [e["payload"]["step"] for e in events if e["type"] == "step_started"]
    assert step_names == ["context-loader", "intent-clarifier"]


def test_executor_renders_prompt_template(tmp_path: Path) -> None:
    from collections.abc import Iterator
    from typing import Any

    from imp.agents.runner import AgentInvocation
    from imp.engine.change import Change
    from imp.engine.executor import execute_step
    from imp.engine.state import State
    from imp.engine.types import (
        AgentDef,
        OutputField,
        OutputSchema,
        SourceLoc,
        Step,
    )

    captured: list[AgentInvocation] = []

    class _Capture:
        def run(self, inv: AgentInvocation) -> Iterator[dict[str, Any]]:
            captured.append(inv)
            yield {"type": "agent_output", "payload": {"status": "Success"}}

    change_dir = tmp_path / ".imp" / "changes" / "1"
    (change_dir / "artifacts").mkdir(parents=True)
    (change_dir / "state").mkdir()
    (change_dir / "change.json").write_text(
        '{"id": 1, "title": "MyChange", "created_at": "2026-01-01"}'
    )
    change = Change(
        id=1, title="MyChange", project_root=tmp_path, change_dir=change_dir,
    )
    step = Step(
        name="x", file=tmp_path / "x.md", interactive=False,
        model_role="default", fs_permissions=(), gates=(),
        max_retries=0, on_max_retries=None,  # type: ignore[arg-type]
        source=SourceLoc(file=tmp_path / "x.md", line=1),
    )
    agent_def = AgentDef(
        name="x", description="", tools=frozenset(), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum", values=("Success",)),),
            source=SourceLoc(file=tmp_path / "x.md", line=1),
        ),
        prompt_template_source="Hello from {{ change.title }} (#{{ change.id }})",
        source_path=tmp_path / "x.md",
        source=SourceLoc(file=tmp_path / "x.md", line=1),
    )
    state = State(
        change_id=1, phase="p", step=None, step_attempt=0,
        phase_attempt=0, status="running",
    )
    execute_step(step=step, agent_def=agent_def, change=change,
                 runner=_Capture(), state=state)
    assert captured
    assert captured[0].system_prompt == "Hello from MyChange (#1)"
    assert len(captured[0].initial_messages) == 1


def test_phase_gate_can_force_pause(tmp_path: Path) -> None:
    ch = _seed(
        tmp_path,
        workflow_yaml=(
            "name: t\ndescription: \"\"\nphases: [p, q]\n"
            "p:\n"
            "  steps: [{name: context-loader, file: agents/context-loader.md}]\n"
            "  gates:\n"
            "    - type: script\n"
            "      command: \"false\"\n"
            "      decide:\n"
            "        - {when: {exit: 0}, action: {kind: continue}}\n"
            "        - {else: true, action: {kind: pause}}\n"
            "q:\n  steps: [{name: context-loader, file: agents/context-loader.md}]\n"
        ),
        agents={"context-loader.md": _STUB_FM},
    )
    wf = load_workflow(ch.workflow_dir)
    runner = StubAgentRunner(canned_path=FIXTURES / "canned" / "stub_outputs.yaml")
    state = State.load(ch.state_dir)
    action = execute_phase(
        phase=wf.phases[0], workflow=wf, change=ch, runner=runner, state=state,
    )
    assert isinstance(action, Pause)
