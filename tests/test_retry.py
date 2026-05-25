from pathlib import Path

from imp.agents.stub_runner import StubAgentRunner
from imp.engine.change import Change
from imp.engine.executor import RunResult, run
from imp.engine.state import State
from tests.conftest import write_agent_md

FIXTURES = Path(__file__).parent / "fixtures"


def _seed(tmp: Path, wf: str, agents: dict[str, str]) -> Change:
    src = tmp / ".imp" / "workflows" / "default"
    (src / "agents").mkdir(parents=True)
    for name, fm in agents.items():
        write_agent_md(src / "agents" / name, frontmatter=fm)
    (src / "workflow.yaml").write_text(wf)
    (tmp / ".imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: [{provider: anthropic, model: x}]}\n"
    )
    return Change.create(tmp, title="t")


_SUCCESS_FM = (
    "name: context-loader\ntools: [complete_step]\n"
    "output:\n  status: {type: enum, values: [Success, Error]}\n"
    "  notes: {type: string, required: false}"
)
_FAILING_FM = (
    "name: failing-stub\ntools: [complete_step]\n"
    "output:\n  status: {type: enum, values: [Success, Error]}"
)


def test_run_completes_simple_workflow(tmp_path: Path) -> None:
    ch = _seed(
        tmp_path,
        wf=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "p:\n  steps: [{name: context-loader, file: agents/context-loader.md}]\n"
        ),
        agents={"context-loader.md": _SUCCESS_FM},
    )
    runner = StubAgentRunner(canned_path=FIXTURES / "canned" / "stub_outputs.yaml")
    result = run(ch, runner)
    assert result is RunResult.COMPLETED
    state = State.load(ch.state_dir)
    assert state.status == "completed"


def test_run_pauses_on_pause_action(tmp_path: Path) -> None:
    ch = _seed(
        tmp_path,
        wf=(
            "name: t\ndescription: \"\"\nphases: [p, q]\n"
            "p:\n  steps: [{name: context-loader, file: agents/context-loader.md}]\n"
            "  gates:\n"
            "    - type: script\n"
            "      command: \"false\"\n"
            "      decide:\n"
            "        - {when: {exit: 0}, action: {kind: continue}}\n"
            "        - {else: true, action: {kind: pause}}\n"
            "q:\n  steps: [{name: context-loader, file: agents/context-loader.md}]\n"
        ),
        agents={"context-loader.md": _SUCCESS_FM},
    )
    runner = StubAgentRunner(canned_path=FIXTURES / "canned" / "stub_outputs.yaml")
    result = run(ch, runner)
    assert result is RunResult.PAUSED
    state = State.load(ch.state_dir)
    assert state.status == "paused"


def test_run_goto_jumps_phase(tmp_path: Path) -> None:
    ch = _seed(
        tmp_path,
        wf=(
            "name: t\ndescription: \"\"\nphases: [a, b]\n"
            "defaults:\n  max_retries: 1\n  on_max_retries: {kind: pause}\n"
            "a:\n  steps: [{name: context-loader, file: agents/context-loader.md}]\n"
            "  gates:\n"
            "    - type: script\n"
            "      command: \"true\"\n"
            "      decide:\n"
            "        - {when: {exit: 0}, action: {kind: goto, target: b}}\n"
            "        - {else: true, action: {kind: pause}}\n"
            "b:\n  steps: [{name: context-loader, file: agents/context-loader.md}]\n"
        ),
        agents={"context-loader.md": _SUCCESS_FM},
    )
    runner = StubAgentRunner(canned_path=FIXTURES / "canned" / "stub_outputs.yaml")
    result = run(ch, runner)
    assert result is RunResult.COMPLETED


def test_step_max_retries_exhausts_to_pause(tmp_path: Path) -> None:
    ch = _seed(
        tmp_path,
        wf=(
            "name: t\ndescription: \"\"\nphases: [p]\n"
            "defaults:\n  max_retries: 2\n  on_max_retries: {kind: pause}\n"
            "p:\n  steps:\n"
            "    - {name: failing-stub, file: agents/failing-stub.md}\n"
        ),
        agents={"failing-stub.md": _FAILING_FM},
    )
    runner = StubAgentRunner(canned_path=FIXTURES / "canned" / "stub_outputs.yaml")
    result = run(ch, runner)
    assert result is RunResult.PAUSED
    events = list(ch.event_log().read())
    # Step ran 3 times: initial + 2 retries.
    step_starts = [e for e in events if e["type"] == "step_started"
                   and e["payload"]["step"] == "failing-stub"]
    assert len(step_starts) == 3
