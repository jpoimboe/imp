# tests/test_gates.py — rewrite
import os
import shlex
import subprocess
import sys
import time
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


def test_step_visibility_and_output_defaults() -> None:
    """New Step fields default to the additive, no-op values (frame on, body off,
    no typed output channel) so every existing step keeps its current behavior."""
    step = _script_step("echo hi")
    assert step.show_label is True
    assert step.show_output is False
    assert step.output_schema is None


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
                        setenv=(("IMP_CHANGE_ID", ""),), network=True, note="")
    step = _script_step("echo hi")
    run_script(step, cwd=tmp_path, policy=pol,
               env_extra={"IMP_CHANGE_ID": "realid01"})
    argv = captured["argv"]
    assert argv.count("IMP_CHANGE_ID") == 1
    assert argv[argv.index("IMP_CHANGE_ID") + 1] == "realid01"


def test_run_script_timeout_kills_group(tmp_path) -> None:
    # A gate exceeding its timeout is killed; no orphan. Bare path (no policy).
    from imp.engine.gates import run_script
    step = _script_step("sleep 30", timeout_seconds=1)
    with pytest.raises(subprocess.TimeoutExpired):
        run_script(step, cwd=tmp_path)


def test_route_script_does_not_block_event_loop(tmp_path) -> None:
    """Blocking subprocess must run off the event loop (regression: frozen live-status tick)."""
    import inspect
    import types

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.state import InMemoryEventLog

    change = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        change_dir=tmp_path / ".imp" / "change",
    )

    async def main() -> None:
        state = types.SimpleNamespace(vars={}, route_label=None)
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


def test_route_script_goto_exit0_suppresses_feedback(tmp_path) -> None:
    """exit-0 Goto carries no feedback; success routes must not inject retry framing."""
    import types

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.state import InMemoryEventLog

    change = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        change_dir=tmp_path / ".imp" / "change",
    )
    step = Step(
        name="autosquash", phase="p", kind="script",
        rules=(_rule(None, Goto("review")),),
        on_max_retries=Pause(), max_loops=10, source=loc(),
        command="echo 'no fixup!/squash!/amend! commits to autosquash'; exit 0",
    )

    async def main() -> None:
        state = types.SimpleNamespace(vars={}, route_label=None)
        log = InMemoryEventLog()
        action, feedback, _ = await _route_script(step, change, state, log)
        assert isinstance(action, Goto)
        assert feedback is None

    anyio.run(main)


def test_route_script_goto_nonzero_keeps_feedback(tmp_path) -> None:
    """Non-zero exit Goto carries retry feedback: 'not accepted' and 'failed (exit N)'."""
    import types

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.state import InMemoryEventLog

    change = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        change_dir=tmp_path / ".imp" / "change",
    )
    step = Step(
        name="check", phase="p", kind="script",
        rules=(_rule(None, Goto("spec-writer")),),
        on_max_retries=Pause(), max_loops=10, source=loc(),
        command="echo boom; exit 3",
    )

    async def main() -> None:
        state = types.SimpleNamespace(vars={}, route_label=None)
        log = InMemoryEventLog()
        action, feedback, _ = await _route_script(step, change, state, log)
        assert isinstance(action, Goto)
        assert feedback is not None
        assert "failed (exit 3)" in feedback
        assert "not accepted" in feedback

    anyio.run(main)


def test_run_script_rstrips_stdout(tmp_path) -> None:
    """Gate stdout exposed to rules/feedback has trailing whitespace trimmed."""
    step = _script_step("printf 'abc123\\n\\n'")
    out = run_script(step, cwd=tmp_path)
    assert out["stdout"] == "abc123"


def test_run_script_does_not_rstrip_stderr(tmp_path) -> None:
    """stderr is returned verbatim; only stdout is rstripped."""
    step = _script_step("echo err >&2; printf 'out'")
    out = run_script(step, cwd=tmp_path)
    assert out["stderr"] == "err\n"
    assert out["stdout"] == "out"


def test_run_script_uses_command_override(tmp_path) -> None:
    """When a rendered command is supplied, run_script executes it, not step.command."""
    step = _script_step("echo step-command")
    out = run_script(step, cwd=tmp_path, command="echo override")
    assert out["stdout"] == "override"


def test_route_script_renders_command_template(tmp_path) -> None:
    """Gate command {{ vars.X }} templates are rendered against state.vars before execution."""
    import types

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.state import InMemoryEventLog

    change = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        cur_base_head="basesha", orig_base_head="basesha",
        change_dir=tmp_path / ".imp" / "change",
    )
    step = Step(
        name="g", phase="p", kind="script",
        rules=(_rule(None, Continue()),),
        on_max_retries=Pause(), max_loops=10, source=loc(),
        command="echo {{ vars.review_base }} > rendered.txt",
    )

    async def main() -> None:
        state = types.SimpleNamespace(vars={"review_base": "abc123"}, route_label=None)
        log = InMemoryEventLog()
        await _route_script(step, change, state, log)

    anyio.run(main)
    assert (tmp_path / "rendered.txt").read_text().strip() == "abc123"


def test_route_script_merges_declared_output_into_rules(tmp_path) -> None:
    """A script writing its declared field to $IMP_OUTPUT makes that typed value
    readable in rules alongside exit/stdout/stderr."""
    import types

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.expr import Name
    from imp.engine.state import InMemoryEventLog
    from imp.engine.types import OutputField, OutputSchema

    change = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        change_dir=tmp_path / ".imp" / "change",
    )
    schema = OutputSchema(
        fields=(OutputField(name="task_total", type="int"),), source=loc())
    step = Step(
        name="announce", phase="p", kind="script",
        rules=(_rule(None, Continue(), muts=(("n", Name("task_total")),)),),
        on_max_retries=Pause(), max_loops=10, source=loc(),
        command=r"""echo hi; printf '{"task_total": 5}' > "$IMP_OUTPUT" """,
        output_schema=schema,
    )

    async def main() -> None:
        state = types.SimpleNamespace(vars={"n": 0}, route_label=None)
        log = InMemoryEventLog()
        action, _feedback, _ = await _route_script(step, change, state, log)
        assert isinstance(action, Continue)
        assert state.vars["n"] == 5

    anyio.run(main)


def test_route_script_pauses_on_malformed_output(tmp_path) -> None:
    """A declared output the script fails to satisfy (bad JSON) pauses the step
    with the validation error as feedback, rather than exposing a partial dict."""
    import types

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.state import InMemoryEventLog
    from imp.engine.types import OutputField, OutputSchema

    change = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        change_dir=tmp_path / ".imp" / "change",
    )
    schema = OutputSchema(
        fields=(OutputField(name="task_total", type="int"),), source=loc())
    step = Step(
        name="announce", phase="p", kind="script",
        rules=(_rule(None, Continue()),),
        on_max_retries=Pause(), max_loops=10, source=loc(),
        command=r"""printf 'not json' > "$IMP_OUTPUT"; exit 0""",
        output_schema=schema,
    )

    async def main() -> None:
        state = types.SimpleNamespace(vars={}, route_label=None)
        log = InMemoryEventLog()
        action, feedback, _ = await _route_script(step, change, state, log)
        assert isinstance(action, Pause)
        assert feedback is not None

    anyio.run(main)


def test_route_script_pauses_on_type_mismatch(tmp_path) -> None:
    """A declared int field the script emits as a string fails schema validation
    and pauses with step_completed + script_blocked events."""
    import types

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.state import InMemoryEventLog
    from imp.engine.types import OutputField, OutputSchema

    change = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        change_dir=tmp_path / ".imp" / "change",
    )
    schema = OutputSchema(
        fields=(OutputField(name="task_total", type="int"),), source=loc())
    step = Step(
        name="announce", phase="p", kind="script",
        rules=(_rule(None, Continue()),),
        on_max_retries=Pause(), max_loops=10, source=loc(),
        command=r"""printf '{"task_total": "nope"}' > "$IMP_OUTPUT"; exit 0""",
        output_schema=schema,
    )

    async def main() -> None:
        state = types.SimpleNamespace(vars={}, route_label=None)
        log = InMemoryEventLog()
        action, feedback, _ = await _route_script(step, change, state, log)
        assert isinstance(action, Pause)
        assert feedback is not None
        types_seen = [e["type"] for e in log.read()]
        assert "step_completed" in types_seen
        assert "script_blocked" in types_seen

    anyio.run(main)


def test_route_script_without_output_schema_unchanged(tmp_path) -> None:
    """A script with no output: schema is byte-for-byte unchanged."""
    import types

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.state import InMemoryEventLog

    change = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        change_dir=tmp_path / ".imp" / "change",
    )
    step = Step(
        name="plain", phase="p", kind="script",
        rules=(_rule(None, Continue()),),
        on_max_retries=Pause(), max_loops=10, source=loc(),
        command="echo hi",
    )

    async def main() -> None:
        state = types.SimpleNamespace(vars={}, route_label=None)
        log = InMemoryEventLog()
        action, feedback, _ = await _route_script(step, change, state, log)
        assert isinstance(action, Continue)
        assert feedback is None

    anyio.run(main)


def test_route_script_cleans_up_output_path_on_run_script_error(tmp_path) -> None:
    """Temp $IMP_OUTPUT file is deleted even when run_script raises."""
    import tempfile as _tempfile
    import types
    import unittest.mock as mock

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.state import InMemoryEventLog
    from imp.engine.types import OutputField, OutputSchema

    change = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        change_dir=tmp_path / ".imp" / "change",
    )
    schema = OutputSchema(
        fields=(OutputField(name="task_total", type="int"),), source=loc())
    step = Step(
        name="announce", phase="p", kind="script",
        rules=(_rule(None, Continue()),),
        on_max_retries=Pause(), max_loops=10, source=loc(),
        command="echo hi",
        output_schema=schema,
    )
    created: list[Path] = []
    _original_mkstemp = _tempfile.mkstemp

    def tracking_mkstemp(*args, **kwargs):
        fd, name = _original_mkstemp(*args, **kwargs)
        created.append(Path(name))
        return fd, name

    async def main() -> None:
        state = types.SimpleNamespace(vars={}, route_label=None)
        log = InMemoryEventLog()
        with (
            mock.patch("imp.engine.executor.run_script", side_effect=RuntimeError("timeout")),
            mock.patch("tempfile.mkstemp", side_effect=tracking_mkstemp),
            pytest.raises(RuntimeError, match="timeout"),
        ):
            await _route_script(step, change, state, log)
        assert created, "expected temp file to be created"
        assert not created[0].exists(), "temp file was not cleaned up after run_script raised"

    anyio.run(main)


def test_route_script_emits_script_output_on_show_output_success(tmp_path) -> None:
    import types

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.state import InMemoryEventLog

    change = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        change_dir=tmp_path / ".imp" / "change")
    step = Step(
        name="announce", phase="p", kind="script",
        rules=(_rule(None, Continue()),),
        on_max_retries=Pause(), max_loops=10, source=loc(),
        command='printf "hi there"', show_output=True)

    async def main() -> None:
        state = types.SimpleNamespace(vars={}, route_label=None)
        log = InMemoryEventLog()
        action, _, _ = await _route_script(step, change, state, log)
        assert isinstance(action, Continue)
        outs = [e for e in log.read() if e["type"] == "script_output"]
        assert len(outs) == 1
        assert "hi there" in outs[0]["payload"]["stdout"]

    anyio.run(main)


def test_route_script_no_script_output_on_nonzero_exit(tmp_path) -> None:
    import types

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.state import InMemoryEventLog

    change = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        change_dir=tmp_path / ".imp" / "change")
    step = Step(
        name="announce", phase="p", kind="script",
        rules=(_rule(None, Continue()),),
        on_max_retries=Pause(), max_loops=10, source=loc(),
        command="echo boom; exit 1", show_output=True)

    async def main() -> None:
        state = types.SimpleNamespace(vars={}, route_label=None)
        log = InMemoryEventLog()
        action, _, _ = await _route_script(step, change, state, log)
        assert isinstance(action, Continue)
        assert not any(e["type"] == "script_output" for e in log.read())

    anyio.run(main)


def test_graceful_kill_escalates_to_sigkill_when_sigterm_ignored(tmp_path) -> None:
    """A child that ignores SIGTERM is SIGKILLed after the grace window; the
    helper waits the full grace before escalating, reaps the process, and closes
    the parent-side read FDs."""
    from imp.engine.gates import GATE_KILL_GRACE_S, _graceful_kill

    # Write a byte to stdout once SIG_IGN is installed so the parent can
    # synchronize — without this handshake, killpg races Python's ~30 ms startup
    # and the default SIGTERM handler kills the process before SIG_IGN is set.
    prog = ("import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "sys.stdout.write('r'); sys.stdout.flush(); "
            "time.sleep(30)")
    proc = subprocess.Popen(
        [sys.executable, "-c", prog],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    proc.stdout.read(1)  # wait until child has installed SIG_IGN
    t0 = time.monotonic()
    _graceful_kill(proc)
    elapsed = time.monotonic() - t0

    assert elapsed >= GATE_KILL_GRACE_S            # waited the grace before SIGKILL
    assert proc.returncode is not None             # reaped, no zombie
    assert proc.stdout.closed and proc.stderr.closed


def test_graceful_kill_reaps_on_sigterm_without_waiting_grace(tmp_path) -> None:
    """A child that exits on SIGTERM is reaped promptly — the helper does NOT
    burn the full grace window when SIGTERM already worked."""
    from imp.engine.gates import GATE_KILL_GRACE_S, _graceful_kill

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    t0 = time.monotonic()
    _graceful_kill(proc)
    elapsed = time.monotonic() - t0

    assert elapsed < GATE_KILL_GRACE_S             # exited on SIGTERM, no grace burn
    assert proc.returncode is not None
    assert proc.stdout.closed and proc.stderr.closed


def test_run_script_timeout_tears_down_gracefully(tmp_path) -> None:
    """The timeout path now uses graceful teardown: a SIGTERM-ignoring child is
    SIGTERMed (grace waited) before the SIGKILL, so total time is at least the
    timeout plus the grace window. Still raises TimeoutExpired."""
    from imp.engine.gates import GATE_KILL_GRACE_S, run_script

    prog = ("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(30)")
    # `exec` so the traced process IS the SIGTERM-ignoring python (the group
    # leader), not a sh wrapper that dies immediately and short-circuits the wait.
    step = _script_step(f"exec {sys.executable} -c {shlex.quote(prog)}",
                        timeout_seconds=1)
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_script(step, cwd=tmp_path)
    elapsed = time.monotonic() - t0
    assert elapsed >= 1 + GATE_KILL_GRACE_S
