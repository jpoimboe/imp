from pathlib import Path

import pytest

from imp.engine.env import engine_env
from imp.engine.gates import DecideNoMatch, evaluate_decide, run_human_gate, run_script_gate
from imp.engine.types import (
    Action,
    Continue,
    Decide,
    Goto,
    HumanGate,
    HumanOption,
    Pause,
    Retry,
    Rule,
    ScriptGate,
    SourceLoc,
)


def loc(line: int = 1) -> SourceLoc:
    return SourceLoc(file=Path("x"), line=line)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ── Task 16: Decide evaluator ──────────────────────────────────────────────

def test_decide_simple_match() -> None:
    decide = Decide(
        rules=(
            Rule(when=(("status", "Error"),), is_else=False, action=Retry(), source=loc()),
            Rule(when=(), is_else=True, action=Continue(), source=loc()),
        ),
        source=loc(),
    )
    assert isinstance(evaluate_decide(decide, {"status": "Error"}), Retry)
    assert isinstance(evaluate_decide(decide, {"status": "Success"}), Continue)


def test_decide_first_match_wins() -> None:
    decide = Decide(
        rules=(
            Rule(when=(("exit", 0),), is_else=False, action=Continue(), source=loc()),
            Rule(when=(("exit", 0),), is_else=False, action=Pause(), source=loc()),
            Rule(when=(), is_else=True, action=Retry(), source=loc()),
        ),
        source=loc(),
    )
    assert isinstance(evaluate_decide(decide, {"exit": 0, "stdout": "ok"}), Continue)


def test_decide_multi_field_match() -> None:
    decide = Decide(
        rules=(
            Rule(
                when=(("status", "Success"), ("severity", "low")),
                is_else=False,
                action=Continue(),
                source=loc(),
            ),
            Rule(when=(), is_else=True, action=Retry(), source=loc()),
        ),
        source=loc(),
    )
    assert isinstance(
        evaluate_decide(decide, {"status": "Success", "severity": "low"}),
        Continue,
    )
    assert isinstance(
        evaluate_decide(decide, {"status": "Success", "severity": "high"}),
        Retry,
    )


def test_decide_no_match_raises() -> None:
    decide = Decide(
        rules=(Rule(when=(("exit", 0),), is_else=False, action=Continue(), source=loc()),),
        source=loc(),
    )
    with pytest.raises(DecideNoMatch):
        evaluate_decide(decide, {"exit": 1})


def test_decide_goto_action() -> None:
    decide = Decide(
        rules=(
            Rule(when=(), is_else=True, action=Goto(target="spec"), source=loc()),
        ),
        source=loc(),
    )
    action = evaluate_decide(decide, {})
    assert isinstance(action, Goto)
    assert action.target == "spec"


# ── Task 17: Script gate runner ────────────────────────────────────────────

def _script_gate(cmd: str, decide: Decide, cwd: str | None = None) -> ScriptGate:
    return ScriptGate(
        command=cmd, cwd=cwd, timeout_seconds=None, decide=decide, source=loc(),
    )


def test_script_gate_success(tmp_path: Path) -> None:
    decide = Decide(
        rules=(
            Rule(when=(("exit", 0),), is_else=False, action=Continue(), source=loc()),
            Rule(when=(), is_else=True, action=Retry(), source=loc()),
        ),
        source=loc(),
    )
    gate = _script_gate("true", decide)
    action, context = run_script_gate(gate, cwd=tmp_path)
    assert isinstance(action, Continue)
    assert context["exit"] == 0


def test_script_gate_failure(tmp_path: Path) -> None:
    decide = Decide(
        rules=(
            Rule(when=(("exit", 0),), is_else=False, action=Continue(), source=loc()),
            Rule(when=(), is_else=True, action=Retry(), source=loc()),
        ),
        source=loc(),
    )
    gate = _script_gate("false", decide)
    action, context = run_script_gate(gate, cwd=tmp_path)
    assert isinstance(action, Retry)
    assert context["exit"] != 0


def test_script_gate_captures_stdout(tmp_path: Path) -> None:
    decide = Decide(
        rules=(Rule(when=(), is_else=True, action=Continue(), source=loc()),),
        source=loc(),
    )
    gate = _script_gate("echo hello", decide)
    _, context = run_script_gate(gate, cwd=tmp_path)
    assert "hello" in context["stdout"]


def test_script_gate_with_explicit_cwd(tmp_path: Path) -> None:
    (tmp_path / "marker").write_text("here")
    decide = Decide(
        rules=(Rule(when=(), is_else=True, action=Continue(), source=loc()),),
        source=loc(),
    )
    gate = ScriptGate(
        command="cat marker",
        cwd=str(tmp_path),
        timeout_seconds=None,
        decide=decide,
        source=loc(),
    )
    _, context = run_script_gate(gate, cwd=Path("/tmp"))  # gate.cwd wins
    assert "here" in context["stdout"]


# ── Task 18: Human gate runner ─────────────────────────────────────────────

def _human_gate(*pairs: tuple[str, Action]) -> HumanGate:
    return HumanGate(
        prompt="continue?",
        options=tuple(
            HumanOption(label=lbl, action=act, source=loc()) for lbl, act in pairs
        ),
        source=loc(),
    )


@pytest.mark.anyio
async def test_human_gate_match_by_number() -> None:
    gate = _human_gate(("Yes", Continue()), ("No", Pause()))
    inputs = iter(["1"])
    out: list[str] = []
    action, chosen = await run_human_gate(
        gate, input_fn=lambda: next(inputs), output_fn=out.append
    )
    assert isinstance(action, Continue)
    assert chosen.label == gate.options[0].label


@pytest.mark.anyio
async def test_human_gate_match_by_label_prefix() -> None:
    gate = _human_gate(("Approve", Continue()), ("Reject", Pause()))
    inputs = iter(["app"])
    action, _chosen = await run_human_gate(
        gate, input_fn=lambda: next(inputs), output_fn=lambda _s: None
    )
    assert isinstance(action, Continue)


@pytest.mark.anyio
async def test_human_gate_reprompts_on_ambiguous() -> None:
    gate = _human_gate(
        ("Approve", Continue()), ("Approve-with-edits", Retry()),
    )
    inputs = iter(["app", "Approve"])  # 1st ambiguous, 2nd exact
    out: list[str] = []
    action, _chosen = await run_human_gate(
        gate, input_fn=lambda: next(inputs), output_fn=out.append
    )
    assert isinstance(action, Continue)
    assert any("ambiguous" in s.lower() for s in out)


@pytest.mark.anyio
async def test_human_gate_reprompts_on_invalid() -> None:
    gate = _human_gate(("Yes", Continue()), ("No", Pause()))
    inputs = iter(["maybe", "9", "2"])
    out: list[str] = []
    action, _chosen = await run_human_gate(
        gate, input_fn=lambda: next(inputs), output_fn=out.append
    )
    assert isinstance(action, Pause)


@pytest.mark.anyio
async def test_human_gate_renders_prompt_and_options() -> None:
    gate = _human_gate(("Approve", Continue()), ("Retry", Retry()))
    out: list[str] = []
    await run_human_gate(
        gate, input_fn=lambda: "1", output_fn=out.append,
    )
    rendered = "\n".join(out)
    assert "continue?" in rendered.lower() or "human gate" in rendered.lower()
    assert "1) Approve" in rendered
    assert "2) Retry" in rendered


# ── Issue 2 Part C: env_extra injection ───────────────────────────────────────

def test_script_gate_receives_env_extra(tmp_path: Path) -> None:
    """env_extra is merged into the subprocess environment."""
    decide = Decide(
        rules=(
            Rule(when=(("exit", 0),), is_else=False, action=Continue(), source=loc()),
            Rule(when=(), is_else=True, action=Retry(), source=loc()),
        ),
        source=loc(),
    )
    gate = _script_gate('test "$IMP_ARTIFACTS_DIR" = "/tmp/probe"', decide)
    action, ctx = run_script_gate(
        gate, cwd=tmp_path, env_extra={"IMP_ARTIFACTS_DIR": "/tmp/probe"},
    )
    assert ctx["exit"] == 0  # test command passed → env var was set correctly
    assert isinstance(action, Continue)


def test_script_gate_receives_project_root_from_engine_env(tmp_path: Path) -> None:
    """engine_env() output, fed to the gate, exposes IMP_PROJECT_ROOT — the var
    run-tests.sh `cd`s into. Guards the dogfood blocker fix."""
    decide = Decide(
        rules=(
            Rule(when=(("exit", 0),), is_else=False, action=Continue(), source=loc()),
            Rule(when=(), is_else=True, action=Retry(), source=loc()),
        ),
        source=loc(),
    )
    gate = _script_gate(f'test "$IMP_PROJECT_ROOT" = "{tmp_path}"', decide)
    env_extra = engine_env(project_root=tmp_path, artifacts_dir=tmp_path / "a")
    action, ctx = run_script_gate(gate, cwd=tmp_path, env_extra=env_extra)
    assert ctx["exit"] == 0
    assert isinstance(action, Continue)


def test_script_gate_resolves_relative_command_against_cwd(tmp_path: Path) -> None:
    """Bug L: when workflow.yaml says `command: ./scripts/lint.sh`, the
    engine passes cwd=change.workflow_dir so the relative path resolves
    against the workflow snapshot (where scripts live), not project_root.
    Without this, the gate would crash with exit 127 (command not found)."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "lint.sh").write_text("#!/usr/bin/env bash\necho ok\n")
    (scripts / "lint.sh").chmod(0o755)
    decide = Decide(
        rules=(
            Rule(when=(("exit", 0),), is_else=False, action=Continue(), source=loc()),
            Rule(when=(), is_else=True, action=Retry(), source=loc()),
        ),
        source=loc(),
    )
    gate = _script_gate("./scripts/lint.sh", decide)
    action, ctx = run_script_gate(gate, cwd=tmp_path)
    assert ctx["exit"] == 0, f"got exit {ctx['exit']}; stderr={ctx['stderr']!r}"
    assert isinstance(action, Continue)
