# tests/test_gates.py — rewrite
import subprocess
from pathlib import Path

import pytest

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
                rules=(_rule(None, Continue()),), options=(),
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


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_run_human_match_by_number() -> None:
    """run_human returns the chosen HumanOption matched by 1-based index."""
    from imp.engine.gates import run_human
    from imp.engine.types import HumanOption

    opts = (
        HumanOption(label="Approve", action=Continue(), mutations=(), source=loc()),
        HumanOption(label="Reject", action=Pause(), mutations=(), source=loc()),
    )
    step = Step(name="review", phase="p", kind="human",
                rules=(), options=opts,
                on_max_retries=Pause(), max_loops=10, source=loc(),
                ask="What now?")
    inputs = iter(["1"])
    out: list[str] = []
    chosen = await run_human(step, input_fn=lambda: next(inputs), output_fn=out.append)
    assert isinstance(chosen.action, Continue)
    assert chosen.label == "Approve"


def _script_step(command: str, timeout_seconds: int | None = None) -> "Step":
    """Minimal Step for script-kind tests."""
    from imp.engine.types import Pause, SourceLoc, Step
    return Step(
        name="g", phase="p", kind="script",
        rules=(), options=(), on_max_retries=Pause(), max_loops=10,
        source=SourceLoc(file=Path("x"), line=1),
        command=command, timeout_seconds=timeout_seconds,
    )


def test_run_script_bare_when_no_policy(tmp_path) -> None:
    from imp.engine.gates import run_script
    step = _script_step("echo hi")
    out = run_script(step, cwd=tmp_path)        # policy defaults to None → bare
    assert out["exit"] == 0 and "hi" in out["stdout"]


def test_run_script_wraps_with_active_policy(tmp_path, monkeypatch) -> None:
    import imp.engine.gates as gatesmod
    from imp.engine.sandbox import SandboxPolicy

    captured: dict[str, object] = {}
    real_popen = gatesmod.subprocess.Popen

    class FakePopen:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            self._inner = real_popen(["true"], **{k: v for k, v in kw.items()
                                                  if k in ("cwd", "env", "start_new_session",
                                                           "stdout", "stderr", "text")})

        def communicate(self, timeout=None):
            return self._inner.communicate(timeout=timeout)

        @property
        def returncode(self):
            return self._inner.returncode

        @property
        def pid(self):
            return self._inner.pid

    monkeypatch.setattr(gatesmod.subprocess, "Popen", FakePopen)
    pol = SandboxPolicy(active=True, worktree=str(tmp_path),
                        git_common_dir=str(tmp_path), git_ro_remask=(),
                        writable_caches=(), extra_readable=(), extra_writable=(),
                        tmp_host_dir=str(tmp_path), secret_dirs=(), secret_files=(),
                        setenv=(), network=True, note="")
    step = _script_step("echo hi")
    run_script(step, cwd=tmp_path, policy=pol)
    assert captured["argv"][0] == "bwrap"


def test_run_script_timeout_kills_group(tmp_path) -> None:
    # A gate exceeding its timeout is killed; no orphan. Bare path (no policy).
    from imp.engine.gates import run_script
    step = _script_step("sleep 30", timeout_seconds=1)
    with pytest.raises(subprocess.TimeoutExpired):
        run_script(step, cwd=tmp_path)


@pytest.mark.anyio
async def test_run_human_reprompts_on_empty_then_matches() -> None:
    """run_human re-prompts on empty input, then returns on valid selection."""
    from imp.engine.gates import run_human
    from imp.engine.types import HumanOption

    opts = (
        HumanOption(label="Yes", action=Continue(), mutations=(), source=loc()),
        HumanOption(label="No", action=Pause(), mutations=(), source=loc()),
    )
    step = Step(name="confirm", phase="p", kind="human",
                rules=(), options=opts,
                on_max_retries=Pause(), max_loops=10, source=loc(),
                ask="Proceed?")
    inputs = iter(["", "2"])
    out: list[str] = []
    chosen = await run_human(step, input_fn=lambda: next(inputs), output_fn=out.append)
    assert isinstance(chosen.action, Pause)
    assert any("empty" in s.lower() for s in out)
