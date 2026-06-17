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


def _script_step(command: str, timeout_seconds: int | None = None) -> "Step":
    """Minimal Step for script-kind tests."""
    from imp.engine.types import Pause, SourceLoc, Step
    return Step(
        name="g", phase="p", kind="script",
        rules=(), on_max_retries=Pause(), max_loops=10,
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
                        secret_dirs=(), secret_files=(),
                        setenv=(), network=True, note="")
    step = _script_step("echo hi")
    run_script(step, cwd=tmp_path, policy=pol)
    assert captured["argv"][0] == "bwrap"


def test_run_script_refreshes_stale_imp_slug_in_sandbox(tmp_path, monkeypatch) -> None:
    # Regression: the sandbox policy is frozen once at run start with an empty
    # name-on-intent slug; a post-rename gate passes a fresh env_extra. run_script
    # must thread env_extra into the sandbox so the gate sees the real slug, not "".
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
                        secret_dirs=(), secret_files=(),
                        setenv=(("IMP_CHANGE_HANDLE", ""),), network=True, note="")
    step = _script_step("echo hi")
    run_script(step, cwd=tmp_path, policy=pol,
               env_extra={"IMP_CHANGE_HANDLE": "real-handle"})
    argv = captured["argv"]
    assert argv.count("IMP_CHANGE_HANDLE") == 1
    assert argv[argv.index("IMP_CHANGE_HANDLE") + 1] == "real-handle"


def test_run_script_timeout_kills_group(tmp_path) -> None:
    # A gate exceeding its timeout is killed; no orphan. Bare path (no policy).
    from imp.engine.gates import run_script
    step = _script_step("sleep 30", timeout_seconds=1)
    with pytest.raises(subprocess.TimeoutExpired):
        run_script(step, cwd=tmp_path)


def test_route_script_does_not_block_event_loop(tmp_path) -> None:
    """A script gate's blocking subprocess must run OFF the event loop so
    concurrent tasks keep running while it executes.

    Regression for the frozen live status line ("running · 0s") during a
    multi-minute verify gate: `_route_script` ran the subprocess via a
    synchronous `proc.communicate()` on the event-loop thread, starving every
    concurrent anyio task (the status `tick()` animator, the signal receiver,
    the remote-channel poller, live-view event processing) for the command's
    entire duration. This drives a `sleep` gate while a ticker counts loop
    iterations and asserts the loop kept turning.
    """
    import inspect
    import types

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.state import InMemoryEventLog

    change = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
    )

    async def main() -> None:
        state = types.SimpleNamespace(vars={})
        log = InMemoryEventLog()
        step = _script_step("sleep 0.5")

        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await anyio.sleep(0.02)
                ticks += 1

        async with anyio.create_task_group() as tg:
            tg.start_soon(ticker)
            await anyio.sleep(0.06)          # let the ticker spin up
            before = ticks
            res = _route_script(step, change, state, log)
            if inspect.isawaitable(res):
                res = await res
            during = ticks - before
            tg.cancel_scope.cancel()

        # ~0.5s / 0.02s ≈ 25 ticks if the loop kept turning; ≈0 if it was
        # starved by a blocking subprocess on the loop thread.
        assert during >= 8, (
            f"event loop starved during script gate: only {during} ticks "
            "advanced while a 0.5s subprocess ran (expected ~25)"
        )

    anyio.run(main)
