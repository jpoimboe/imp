# tests/test_gates.py — rewrite
import contextlib
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from imp.engine.env import overlay_engine_env
from imp.engine.expr import Binary, BoolLit, Name, StrLit
from imp.engine.gates import evaluate_rules, run_script
from imp.engine.types import Continue, Goto, Pause, Rule, SourceLoc, Step
from imp.paths import project_state_dir
from tests.conftest import _pid_gone_or_zombie


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
                                                           "stdout", "stderr")})

        @property
        def stdout(self):
            return self._inner.stdout

        @property
        def stderr(self):
            return self._inner.stderr

        def poll(self):
            return self._inner.poll()

        def wait(self, timeout=None):
            return self._inner.wait(timeout)

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
                                                           "stdout", "stderr")})

        @property
        def stdout(self):
            return self._inner.stdout

        @property
        def stderr(self):
            return self._inner.stderr

        def poll(self):
            return self._inner.poll()

        def wait(self, timeout=None):
            return self._inner.wait(timeout)

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
                        setenv=(("IMP_QUEST_ID", ""),), network=True, note="")
    step = _script_step("echo hi")
    run_script(step, cwd=tmp_path, policy=pol,
               env_extra={"IMP_QUEST_ID": "realid01"})
    argv = captured["argv"]
    assert argv.count("IMP_QUEST_ID") == 1
    assert argv[argv.index("IMP_QUEST_ID") + 1] == "realid01"


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

    quest = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest",
        agent_quest_home=".imp/quests/test-quest", tmp_dir=tmp_path,
        control_root=None,
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
            res = _route_script(step, quest, state, log)
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

    quest = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest",
        agent_quest_home=".imp/quests/test-quest", tmp_dir=tmp_path,
        control_root=None,
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
        action, feedback, _ = await _route_script(step, quest, state, log)
        assert isinstance(action, Goto)
        assert feedback is None

    anyio.run(main)


def test_route_script_goto_nonzero_keeps_feedback(tmp_path) -> None:
    """Non-zero exit Goto carries retry feedback: 'not accepted' and 'failed (exit N)'."""
    import types

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.state import InMemoryEventLog

    quest = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest",
        agent_quest_home=".imp/quests/test-quest", tmp_dir=tmp_path,
        control_root=None,
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
        action, feedback, _ = await _route_script(step, quest, state, log)
        assert isinstance(action, Goto)
        assert feedback is not None
        assert "failed (exit 3)" in feedback
        assert "not accepted" in feedback

    anyio.run(main)


def test_route_script_goto_exit0_forwards_configured_feedback(tmp_path) -> None:
    """A configured feedback: on an exit-0 forward goto is delivered verbatim."""
    import types

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.state import InMemoryEventLog

    quest = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest",
        agent_quest_home=".imp/quests/test-quest", tmp_dir=tmp_path,
        control_root=None,
    )
    step = Step(
        name="scope-code-check", phase="p", kind="script",
        rules=(_rule(None, Goto(
            "plan-writer",
            feedback="Plan this from the spec drafted with the user:")),),
        on_max_retries=Pause(), max_loops=10, source=loc(),
        command="exit 0",
    )

    async def main() -> None:
        state = types.SimpleNamespace(vars={}, route_label=None)
        log = InMemoryEventLog()
        action, feedback, _ = await _route_script(step, quest, state, log)
        assert isinstance(action, Goto)
        assert feedback == "Plan this from the spec drafted with the user:"

    anyio.run(main)


def test_route_script_nonzero_synthesizes_despite_configured_feedback(tmp_path) -> None:
    """On a non-zero exit the synthesized script output is delivered even when the matched
    rule carries a configured feedback: — the failure diagnostics that the converge-verify
    and integration-verify triage agents reason over (and are forbidden to re-run) must
    never be replaced by the static preamble."""
    import types

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.state import InMemoryEventLog

    quest = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest",
        agent_quest_home=".imp/quests/test-quest", tmp_dir=tmp_path,
        control_root=None,
    )
    preamble = "Integration tests failed on the merged tree. Adjudicate fix-vs-tolerate:"
    step = Step(
        name="integration-verify", phase="p", kind="script",
        rules=(_rule(None, Goto("integration-reviewer", feedback=preamble)),),
        on_max_retries=Pause(), max_loops=10, source=loc(),
        command="echo boom; exit 1",
    )

    async def main() -> None:
        state = types.SimpleNamespace(vars={}, route_label=None)
        log = InMemoryEventLog()
        action, feedback, _ = await _route_script(step, quest, state, log)
        assert isinstance(action, Goto)
        assert feedback is not None
        assert "boom" in feedback            # actual failure output delivered
        assert "failed (exit 1)" in feedback
        assert feedback != preamble          # NOT replaced by the configured preamble

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

    quest = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        cur_base_head="basesha", orig_base_head="basesha",
        base_branch="main", materialized=True,
        quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest",
        agent_quest_home=".imp/quests/test-quest",
        control_root=None,
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
        await _route_script(step, quest, state, log)

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

    quest = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest",
        agent_quest_home=".imp/quests/test-quest", tmp_dir=tmp_path,
        control_root=None,
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
        action, _feedback, _ = await _route_script(step, quest, state, log)
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

    quest = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest",
        agent_quest_home=".imp/quests/test-quest", tmp_dir=tmp_path,
        control_root=None,
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
        action, feedback, _ = await _route_script(step, quest, state, log)
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

    quest = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest",
        agent_quest_home=".imp/quests/test-quest", tmp_dir=tmp_path,
        control_root=None,
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
        action, feedback, _ = await _route_script(step, quest, state, log)
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

    quest = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest",
        agent_quest_home=".imp/quests/test-quest", tmp_dir=tmp_path,
        control_root=None,
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
        action, feedback, _ = await _route_script(step, quest, state, log)
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

    quest = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest",
        agent_quest_home=".imp/quests/test-quest", tmp_dir=tmp_path,
        control_root=None,
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
            await _route_script(step, quest, state, log)
        assert created, "expected temp file to be created"
        assert not created[0].exists(), "temp file was not cleaned up after run_script raised"

    anyio.run(main)


def test_route_script_emits_script_output_on_show_output_success(tmp_path) -> None:
    import types

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.state import InMemoryEventLog

    quest = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest",
        agent_quest_home=".imp/quests/test-quest", control_root=None)
    step = Step(
        name="announce", phase="p", kind="script",
        rules=(_rule(None, Continue()),),
        on_max_retries=Pause(), max_loops=10, source=loc(),
        command='printf "hi there"', show_output=True)

    async def main() -> None:
        state = types.SimpleNamespace(vars={}, route_label=None)
        log = InMemoryEventLog()
        action, _, _ = await _route_script(step, quest, state, log)
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

    quest = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest",
        agent_quest_home=".imp/quests/test-quest", control_root=None)
    step = Step(
        name="announce", phase="p", kind="script",
        rules=(_rule(None, Continue()),),
        on_max_retries=Pause(), max_loops=10, source=loc(),
        command="echo boom; exit 1", show_output=True)

    async def main() -> None:
        state = types.SimpleNamespace(vars={}, route_label=None)
        log = InMemoryEventLog()
        action, _, _ = await _route_script(step, quest, state, log)
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


def test_graceful_kill_escalates_when_group_leader_exits_first(tmp_path) -> None:
    """The sandbox shape, reproduced without bwrap: the group LEADER exits on
    SIGTERM at ~0s while a TERM-ignoring member of its group keeps running.
    Escalation must key on group liveness, not on the leader's exit, or the
    member is leaked."""
    from imp.engine.gates import _graceful_kill

    pidfile = tmp_path / "member.pid"
    # sleep(3600), not sleep(30): the member must outlive the poll budget below by a
    # wide margin, or a leaked member exits on its own and the test passes vacuously.
    prog = ("import os, pathlib, signal, sys, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
            "time.sleep(3600)")
    # `sh` is the group leader and dies on SIGTERM immediately (no trap), exactly
    # like the outer bwrap of a sandboxed gate; the python member ignores SIGTERM.
    proc = subprocess.Popen(
        ["sh", "-c",
         f"{shlex.quote(sys.executable)} -c {shlex.quote(prog)} "
         f"{shlex.quote(str(pidfile))} & wait"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    deadline = time.monotonic() + 20.0        # generous: only bounds process startup
    while not (pidfile.exists() and pidfile.read_text().strip()):
        if time.monotonic() > deadline:
            pytest.fail("the TERM-ignoring group member never started")
        time.sleep(0.02)
    member_pid = int(pidfile.read_text())

    _graceful_kill(proc)

    deadline = time.monotonic() + 20.0        # generous; only burned when the fix is absent
    while not _pid_gone_or_zombie(member_pid):
        if time.monotonic() > deadline:
            with contextlib.suppress(ProcessLookupError):
                os.kill(member_pid, signal.SIGKILL)   # don't leak the runaway on failure
            pytest.fail(f"pid {member_pid} survived teardown — the SIGKILL was skipped")
        time.sleep(0.05)
    assert proc.returncode is not None                # leader reaped, no zombie
    assert proc.stdout.closed and proc.stderr.closed


def test_run_script_abort_kills_group_and_raises(tmp_path) -> None:
    """An abort predicate that flips true mid-run tears down the whole process
    group (background grandchild included) and raises ScriptInterrupted."""
    from imp.engine.gates import ScriptInterrupted, run_script

    pidfile = tmp_path / "child.pid"
    # sh is the group leader; the backgrounded sleep is a group member. Killing
    # the GROUP (not just sh) must reap the grandchild too.
    step = _script_step(f"sleep 30 & echo $! > {shlex.quote(str(pidfile))}; wait")

    calls = {"n": 0}
    def abort() -> bool:
        calls["n"] += 1
        return calls["n"] > 1          # false on the first tick, true after

    with pytest.raises(ScriptInterrupted):
        run_script(step, cwd=tmp_path, abort=abort)

    child_pid = int(pidfile.read_text())
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)      # still alive (or a reaped zombie)
        except ProcessLookupError:
            break                      # gone: the group was torn down
        time.sleep(0.05)
    else:
        pytest.fail("backgrounded child survived the group kill")


def test_run_script_abort_keeps_draining_large_output(tmp_path) -> None:
    """Regression guard against a non-draining wait()/poll() loop: a child writes
    far more than a pipe buffer (~64KB) to stdout, then touches a marker file,
    then sleeps. Only a loop that keeps draining stdout lets the child get PAST
    the blocking write to create the marker. Assert the marker exists after the
    abort — with a non-draining loop the child would still be blocked on the full
    pipe and the marker would never appear."""
    from imp.engine.gates import ScriptInterrupted, run_script

    marker = tmp_path / "marker"
    prog = (
        "import sys, time\n"
        "sys.stdout.write('x' * 200000)\n"
        "sys.stdout.flush()\n"
        "open(sys.argv[1], 'w').write('done')\n"
        "time.sleep(30)\n"
    )
    cmd = f"exec {sys.executable} -c {shlex.quote(prog)} {shlex.quote(str(marker))}"
    step = _script_step(cmd)

    calls = {"n": 0}
    def abort() -> bool:
        calls["n"] += 1
        return calls["n"] > 2          # ~1.5s: ample time for the drained child to progress

    with pytest.raises(ScriptInterrupted):
        run_script(step, cwd=tmp_path, abort=abort)

    assert marker.exists(), "child never progressed past the 200KB write — pipe was not drained"


def test_run_script_no_abort_runs_to_completion(tmp_path) -> None:
    """With abort=None (today's default for every current caller) the poll loop
    ignores interruption and returns the real exit/stdout."""
    from imp.engine.gates import run_script

    step = _script_step("echo done; exit 4")
    out = run_script(step, cwd=tmp_path, abort=None)
    assert out["exit"] == 4
    assert out["stdout"] == "done"


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


def test_route_script_interrupt_returns_pause_no_script_blocked(tmp_path) -> None:
    """An interruptible gate whose run_script raises ScriptInterrupted resolves to
    Pause(interrupt=True): rule evaluation, feedback, and the script_blocked/
    step_completed events are all skipped (a killed run is not a gate verdict)."""
    import types
    import unittest.mock as mock

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.gates import ScriptInterrupted
    from imp.engine.state import InMemoryEventLog

    quest = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest",
        agent_quest_home=".imp/quests/test-quest", tmp_dir=tmp_path,
        control_root=None,
    )
    step = Step(
        name="verify", phase="p", kind="script",
        rules=(_rule(None, Continue()),),
        on_max_retries=Pause(), max_loops=10, source=loc(),
        command="./scripts/run-verify.sh fast", interruptible=True,
    )
    coordinator = types.SimpleNamespace(should_pause=True)

    async def main() -> None:
        state = types.SimpleNamespace(vars={}, route_label=None)
        log = InMemoryEventLog()
        with mock.patch("imp.engine.executor.run_script",
                        side_effect=ScriptInterrupted):
            action, feedback, muts = await _route_script(
                step, quest, state, log, coordinator=coordinator)
        assert isinstance(action, Pause)
        assert action.interrupt is True
        assert feedback is None
        assert muts == {}
        types_seen = [e["type"] for e in log.read()]
        assert "script_blocked" not in types_seen
        assert "step_completed" not in types_seen

    anyio.run(main)


def test_route_script_builds_abort_for_interruptible_step(tmp_path) -> None:
    """For an interruptible step run_script receives a callable abort predicate
    that reads coordinator.should_pause live (not a snapshot)."""
    import types
    import unittest.mock as mock

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.state import InMemoryEventLog

    quest = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest",
        agent_quest_home=".imp/quests/test-quest", tmp_dir=tmp_path,
        control_root=None,
    )
    step = Step(
        name="verify", phase="p", kind="script",
        rules=(_rule(None, Continue()),),
        on_max_retries=Pause(), max_loops=10, source=loc(),
        command="echo hi", interruptible=True,
    )
    coordinator = types.SimpleNamespace(should_pause=False)
    captured: dict[str, object] = {}

    def fake_run_script(*args, **kwargs):
        captured["abort"] = kwargs.get("abort")
        return {"exit": 0, "stdout": "hi", "stderr": ""}

    async def main() -> None:
        state = types.SimpleNamespace(vars={}, route_label=None)
        log = InMemoryEventLog()
        with mock.patch("imp.engine.executor.run_script", side_effect=fake_run_script):
            action, _feedback, _ = await _route_script(
                step, quest, state, log, coordinator=coordinator)
        assert isinstance(action, Continue)
        abort = captured["abort"]
        assert callable(abort)
        assert abort() is False                 # reads should_pause == False
        coordinator.should_pause = True
        assert abort() is True                   # live read, not a snapshot

    anyio.run(main)


def test_route_script_non_interruptible_passes_no_abort(tmp_path) -> None:
    """Regression: a non-interruptible gate (default) passes abort=None and runs
    to completion, ignoring should_pause entirely (today's behavior)."""
    import types
    import unittest.mock as mock

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.state import InMemoryEventLog

    quest = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest",
        agent_quest_home=".imp/quests/test-quest", tmp_dir=tmp_path,
        control_root=None,
    )
    step = Step(
        name="mutating-gate", phase="p", kind="script",
        rules=(_rule(None, Continue()),),
        on_max_retries=Pause(), max_loops=10, source=loc(),
        command="echo hi",                        # interruptible defaults False
    )
    coordinator = types.SimpleNamespace(should_pause=True)
    captured: dict[str, object] = {"abort": "unset"}

    def fake_run_script(*args, **kwargs):
        captured["abort"] = kwargs.get("abort")
        return {"exit": 0, "stdout": "hi", "stderr": ""}

    async def main() -> None:
        state = types.SimpleNamespace(vars={}, route_label=None)
        log = InMemoryEventLog()
        with mock.patch("imp.engine.executor.run_script", side_effect=fake_run_script):
            action, _feedback, _ = await _route_script(
                step, quest, state, log, coordinator=coordinator)
        assert isinstance(action, Continue)
        assert captured["abort"] is None

    anyio.run(main)


def test_run_script_output_cap_kills_group_and_raises(tmp_path, monkeypatch) -> None:
    """A child flooding stdout past GATE_OUTPUT_CAP_BYTES is torn down (group and
    all) and run_script raises ScriptOutputExceeded rather than accumulating
    unboundedly. Proves the bounded-retain property under a fast chatty runaway."""
    import imp.engine.gates as gatesmod
    from imp.engine.gates import ScriptOutputExceeded, run_script

    assert issubclass(ScriptOutputExceeded, subprocess.SubprocessError)
    monkeypatch.setattr(gatesmod, "GATE_OUTPUT_CAP_BYTES", 1_000_000)

    pidfile = tmp_path / "child.pid"
    prog = (
        "import sys\n"
        "buf = b'x' * 65536\n"
        "while True:\n"
        "    sys.stdout.buffer.write(buf)\n"
        "    sys.stdout.buffer.flush()\n"
    )
    # Background a sleeper as a group member so we can prove the WHOLE group is
    # torn down; exec the flooder so it becomes the group leader (was sh).
    cmd = (f"sleep 30 & echo $! > {shlex.quote(str(pidfile))}; "
           f"exec {sys.executable} -c {shlex.quote(prog)}")
    step = _script_step(cmd)

    with pytest.raises(ScriptOutputExceeded):
        run_script(step, cwd=tmp_path)

    child_pid = int(pidfile.read_text())
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("backgrounded child survived the output-cap group kill")


def test_run_script_hard_ceiling_kills_when_no_timeout(tmp_path, monkeypatch) -> None:
    """A step with timeout_seconds=None running a non-exiting child is killed at
    GATE_HARD_CEILING_S and raises subprocess.TimeoutExpired. Tested against a
    reduced ceiling so it runs fast."""
    import imp.engine.gates as gatesmod
    from imp.engine.gates import run_script

    monkeypatch.setattr(gatesmod, "GATE_HARD_CEILING_S", 1)
    step = _script_step("sleep 30", timeout_seconds=None)

    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_script(step, cwd=tmp_path)
    elapsed = time.monotonic() - t0
    assert 1 <= elapsed < 5


def test_run_script_explicit_timeout_beats_hard_ceiling(tmp_path, monkeypatch) -> None:
    """An explicit step.timeout_seconds still wins over the (here large) hard
    ceiling: the step is killed at its own 1s timeout, not the ceiling."""
    import imp.engine.gates as gatesmod
    from imp.engine.gates import run_script

    monkeypatch.setattr(gatesmod, "GATE_HARD_CEILING_S", 3600)
    step = _script_step("sleep 30", timeout_seconds=1)

    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_script(step, cwd=tmp_path)
    elapsed = time.monotonic() - t0
    assert 1 <= elapsed < 5


def test_run_script_decodes_utf8_multibyte(tmp_path) -> None:
    """Binary drain decodes child bytes as utf-8: a multibyte sequence round-trips
    to the correct str (not latin-1 mojibake, not raw bytes)."""
    from imp.engine.gates import run_script

    step = _script_step(r"printf '\303\251'")   # UTF-8 bytes for 'é'
    out = run_script(step, cwd=tmp_path)
    assert out["stdout"] == "é"


def test_run_script_large_output_below_cap_returns_full(tmp_path) -> None:
    """Output well under the cap (but far past a pipe buffer) is drained in full
    without tripping the cap — proves the drain reads past a full pipe."""
    from imp.engine.gates import run_script

    prog = "import sys; sys.stdout.write('y' * 500000)"
    step = _script_step(f"exec {sys.executable} -c {shlex.quote(prog)}")
    out = run_script(step, cwd=tmp_path)
    assert out["exit"] == 0
    assert len(out["stdout"]) == 500000


def test_run_script_prepends_shim_dir_to_path(tmp_path) -> None:
    step = Step(name="g", phase="p", kind="script",
                rules=(_rule(None, Continue()),),
                on_max_retries=Pause(), max_loops=10, source=loc(),
                command='printf %s "$PATH"')
    out = run_script(step, cwd=tmp_path, shim_dir="/q/imp-shim")
    assert out["stdout"].startswith("/q/imp-shim" + os.pathsep)


def test_run_script_no_shim_dir_leaves_path_untouched(tmp_path) -> None:
    step = Step(name="g", phase="p", kind="script",
                rules=(_rule(None, Continue()),),
                on_max_retries=Pause(), max_loops=10, source=loc(),
                command='printf %s "$PATH"')
    out = run_script(step, cwd=tmp_path)
    expected = overlay_engine_env(os.environ, {}).get("PATH", "")
    assert out["stdout"] == expected


def test_route_script_output_tempfile_created_under_quest_tmp_dir(tmp_path, monkeypatch) -> None:
    """The declared-output tempfile ($IMP_OUTPUT) is created under quest.tmp_dir,
    not the ambient system temp — and carries that same real host path into the
    sandbox (no rewriting)."""
    import tempfile as _tempfile
    import types

    import anyio

    from imp.engine import executor
    from imp.engine.executor import _route_script
    from imp.engine.state import InMemoryEventLog
    from imp.engine.types import OutputField, OutputSchema

    qtmp = tmp_path / "qtmp"          # deliberately NOT created — the executor must mkdir it
    quest = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path,
        id="test-id", handle="test", title="Test",
        quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest",
        agent_quest_home=".imp/quests/test-quest", tmp_dir=qtmp,
        control_root=None,
    )
    schema = OutputSchema(
        fields=(OutputField(name="task_total", type="int"),), source=loc())
    step = Step(
        name="announce", phase="p", kind="script",
        rules=(_rule(None, Continue()),),
        on_max_retries=Pause(), max_loops=10, source=loc(),
        command=r"""printf '{"task_total": 5}' > "$IMP_OUTPUT" """,
        output_schema=schema,
    )

    captured: dict[str, object] = {}
    real_mkstemp = _tempfile.mkstemp

    def spy(*a, **k):
        captured["dir"] = k.get("dir")
        return real_mkstemp(*a, **k)

    monkeypatch.setattr(executor.tempfile, "mkstemp", spy)

    async def main() -> None:
        state = types.SimpleNamespace(vars={"n": 0}, route_label=None)
        await _route_script(step, quest, state, InMemoryEventLog())

    anyio.run(main)
    assert captured["dir"] == qtmp   # explicit dir=quest.tmp_dir, not ambient temp
    assert qtmp.is_dir()             # executor created it idempotently


def test_route_script_renders_base_and_checkout_branch(tmp_path) -> None:
    import types

    import anyio

    from imp.engine.executor import _route_script
    from imp.engine.state import InMemoryEventLog
    from tests.conftest import _git, _init_repo

    _init_repo(tmp_path)
    _git(tmp_path, "checkout", "-q", "-b", "release-2.0")   # checkout diverges from base
    quest = types.SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path, control_root=tmp_path,
        id="test-id", handle="test", title="Test",
        base_branch="main", materialized=False,
        cur_base_head="basesha", orig_base_head="basesha",
        quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest",
        agent_quest_home=".imp/quests/test-quest",
    )
    step = Step(
        name="g", phase="p", kind="script",
        rules=(_rule(None, Continue()),),
        on_max_retries=Pause(), max_loops=10, source=loc(),
        # run_script executes with `sh -c`, so the `|` separator must be quoted.
        command='echo "{{ quest.base_branch }}|{{ quest.checkout_branch }}" > rendered.txt',
    )

    async def main() -> None:
        state = types.SimpleNamespace(vars={}, route_label=None)
        await _route_script(step, quest, state, InMemoryEventLog())

    anyio.run(main)
    assert (tmp_path / "rendered.txt").read_text().strip() == "main|release-2.0"


def test_run_command_runs_a_bare_command_without_a_step(tmp_path) -> None:
    from imp.engine.gates import run_command

    out = run_command(command="echo hi; echo bad >&2; exit 3",
                      cwd=str(tmp_path), timeout_seconds=None)
    assert out["exit"] == 3
    assert out["stdout"] == "hi"          # rstripped, like run_script's
    assert out["stderr"] == "bad\n"       # verbatim, like run_script's


def test_run_command_timeout_raises_without_a_step(tmp_path) -> None:
    from imp.engine.gates import run_command

    with pytest.raises(subprocess.TimeoutExpired):
        run_command(command="sleep 30", cwd=str(tmp_path), timeout_seconds=1)


def test_run_command_timeout_carries_partial_output(tmp_path) -> None:
    from imp.engine.gates import run_command

    with pytest.raises(subprocess.TimeoutExpired) as ei:
        run_command(
            command="printf 'partial_stdout'; sleep 30",
            cwd=str(tmp_path),
            timeout_seconds=3,
        )
    assert ei.value.output == "partial_stdout"
    assert ei.value.stderr == ""


def test_run_script_delegates_unpacked_step_fields(tmp_path, monkeypatch) -> None:
    import dataclasses

    import imp.engine.gates as gatesmod

    seen: list[dict] = []

    def _fake(**kw: object) -> dict[str, object]:
        seen.append(kw)
        return {"exit": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(gatesmod, "run_command", _fake)

    step = _script_step("echo from-step", timeout_seconds=7)
    gatesmod.run_script(step, cwd=tmp_path, shim_dir="/q/imp-shim")
    # cwd falls back to the caller's when the step declares none
    assert seen[-1]["command"] == "echo from-step"
    assert seen[-1]["cwd"] == str(tmp_path)
    assert seen[-1]["timeout_seconds"] == 7
    assert seen[-1]["shim_dir"] == "/q/imp-shim"

    pinned = dataclasses.replace(step, cwd="/tmp/step-cwd")
    gatesmod.run_script(pinned, cwd=tmp_path, command="echo override")
    # step.cwd wins over the caller's cwd; the command override wins over step.command
    assert seen[-1]["cwd"] == "/tmp/step-cwd"
    assert seen[-1]["command"] == "echo override"
