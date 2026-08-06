# tests/test_gate_spawn_retry.py
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import anyio

from imp.engine import executor as _executor
from imp.engine.sandbox import SANDBOX_ENTERED_MARKER
from imp.engine.state import InMemoryEventLog
from imp.engine.types import Continue, Goto, Pause, Rule, SourceLoc, Step
from imp.paths import project_state_dir


def _quest(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path, control_root=tmp_path,
        id="test-id", title="Test",
        quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest")


def _step(action) -> Step:
    return Step(
        name="verify", phase="p", kind="script",
        rules=(Rule(condition=None, action=action, mutations=(),
                    source=SourceLoc(file=Path("x"), line=1)),),
        on_max_retries=Pause(), max_loops=10,
        source=SourceLoc(file=Path("x"), line=1),
        command="run-gate", interruptible=False)


def _run(step, quest, policy, log):
    async def main():
        state = SimpleNamespace(vars={}, route_label=None)
        return await _executor._route_script(
            step, quest, state, log, sandbox_policy=policy)
    return anyio.run(main)


def _no_delay(monkeypatch):
    monkeypatch.setattr(_executor, "sandbox_spawn_backoff_delay",
                        lambda attempt: 0.0)


def _warn_msgs(log):
    return [e["payload"]["message"] for e in log.read()
            if e["type"] == "runner_warning"]


def test_gate_retries_then_succeeds(tmp_path, monkeypatch):
    _no_delay(monkeypatch)
    calls = {"n": 0}

    def fake_run_script(step, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:                       # first two spawns fail to enter
            return {"exit": 0, "stdout": "", "stderr": "boom-no-marker"}
        return {"exit": 0, "stdout": "ok",
                "stderr": f"{SANDBOX_ENTERED_MARKER}\nreal"}

    monkeypatch.setattr(_executor, "run_script", fake_run_script)
    log = InMemoryEventLog()
    action, _fb, _m = _run(_step(Continue()), _quest(tmp_path),
                           SimpleNamespace(active=True), log)
    assert isinstance(action, Continue)           # routed on the real exit 0
    assert calls["n"] == 3                         # 2 failed spawns + 1 success
    assert _warn_msgs(log) == [
        "sandbox could not start the step; retrying (2/5)",
        "sandbox could not start the step; retrying (3/5)",
    ]
    warn_events = [e["payload"] for e in log.read()
                   if e["type"] == "runner_warning"]
    assert all(p["reason"] == "sandbox-spawn-failed" for p in warn_events)
    assert all(p["step"] == "verify" for p in warn_events)


def test_gate_persistent_failure_pauses_without_routing(tmp_path, monkeypatch):
    _no_delay(monkeypatch)
    ruled = {"n": 0}
    real_eval = _executor.evaluate_rules
    monkeypatch.setattr(
        _executor, "evaluate_rules",
        lambda *a, **k: (ruled.__setitem__("n", ruled["n"] + 1)
                         or real_eval(*a, **k)))
    monkeypatch.setattr(
        _executor, "run_script",
        lambda step, **kw: {"exit": 0, "stdout": "", "stderr": "boom-no-marker"})
    log = InMemoryEventLog()
    action, _fb, _m = _run(_step(Continue()), _quest(tmp_path),
                           SimpleNamespace(active=True), log)
    assert isinstance(action, Pause)
    assert action.reason == "sandbox_spawn_failed"
    assert ruled["n"] == 0                          # evaluate_rules NEVER called
    types_ = [e["type"] for e in log.read()]
    assert "step_completed" not in types_          # never routed / finished
    assert len(_warn_msgs(log)) == 4               # one breadcrumb per retry (2..5)


_BWRAP_ERR = "bwrap: Can't bind mount /nope: No such file or directory"


def test_gate_retry_warnings_carry_the_sandbox_stderr(tmp_path, monkeypatch):
    # AC 5b: the four retry breadcrumbs precede the pause, so bwrap's own reason
    # is on screen before the quest stops.
    _no_delay(monkeypatch)
    monkeypatch.setattr(
        _executor, "run_script",
        lambda step, **kw: {"exit": 0, "stdout": "",
                            "stderr": f"\n{_BWRAP_ERR}\nnoise below"})
    log = InMemoryEventLog()
    action, _fb, _m = _run(_step(Continue()), _quest(tmp_path),
                           SimpleNamespace(active=True), log)
    assert isinstance(action, Pause)
    details = [e["payload"]["detail"] for e in log.read()
               if e["type"] == "runner_warning"]
    assert details == [_BWRAP_ERR] * 4


def test_gate_healthy_nonzero_routes_and_strips_marker(tmp_path, monkeypatch):
    _no_delay(monkeypatch)
    monkeypatch.setattr(
        _executor, "run_script",
        lambda step, **kw: {"exit": 1, "stdout": "",
                            "stderr": f"{SANDBOX_ENTERED_MARKER}\nboom-detail"})
    log = InMemoryEventLog()
    action, feedback, _m = _run(_step(Goto(target="rework")), _quest(tmp_path),
                                SimpleNamespace(active=True), log)
    assert isinstance(action, Goto)                # routed on the real exit 1
    assert SANDBOX_ENTERED_MARKER not in feedback  # marker stripped before feedback
    assert "boom-detail" in feedback               # real stderr survives the strip
    assert _warn_msgs(log) == []                    # marker present → no retry


def test_gate_syntax_error_routes_as_a_gate_result(tmp_path, monkeypatch):
    # AC 3: the gate's command does not parse, but the wrapper's sentinel is
    # emitted anyway, so the executor evaluates the step's rules on the real exit
    # code instead of pausing with sandbox_spawn_failed.
    from tests.test_sandbox import wrapper_argv
    _no_delay(monkeypatch)
    raw: dict[str, object] = {}

    def run_through_wrapper(step, **kw):
        p = subprocess.run(wrapper_argv('echo "`'), capture_output=True, text=True)
        raw["stderr"], raw["exit"] = p.stderr, p.returncode
        return {"exit": p.returncode, "stdout": p.stdout, "stderr": p.stderr}

    monkeypatch.setattr(_executor, "run_script", run_through_wrapper)
    log = InMemoryEventLog()
    action, feedback, _m = _run(_step(Goto(target="rework")), _quest(tmp_path),
                                SimpleNamespace(active=True), log)
    assert isinstance(action, Goto)                   # routed, not paused
    assert raw["exit"] == 2                           # the shell's own exit code
    assert SANDBOX_ENTERED_MARKER in str(raw["stderr"])   # sentinel survived the bad parse
    assert SANDBOX_ENTERED_MARKER not in feedback     # stripped before the agent sees it
    assert feedback.strip() != ""                     # the shell's diagnosis reached it
    assert _warn_msgs(log) == []                      # no retries emitted


def test_gate_bare_host_never_retries_or_pauses(tmp_path, monkeypatch):
    _no_delay(monkeypatch)
    monkeypatch.setattr(
        _executor, "run_script",
        lambda step, **kw: {"exit": 0, "stdout": "", "stderr": "no marker here"})
    log = InMemoryEventLog()
    # sandbox_policy=None → sandbox_active False → marker-absence is NORMAL, not a failure.
    action, _fb, _m = _run(_step(Continue()), _quest(tmp_path), None, log)
    assert isinstance(action, Continue)
    assert _warn_msgs(log) == []
