# tests/test_gate_spawn_retry.py
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from imp.engine import executor as _executor
from imp.engine.sandbox import (
    SANDBOX_SPAWN_MAX_ATTEMPTS,
    SandboxNamespace,
    SandboxOpenFailed,
    SandboxPolicy,
)
from imp.engine.state import InMemoryEventLog
from imp.engine.types import Continue, Goto, Pause, Rule, SourceLoc, Step
from imp.paths import project_state_dir

_BWRAP_ERR = "bwrap: Can't bind mount /nope: No such file or directory"


def _quest(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_dir=tmp_path, project_root=tmp_path, control_root=tmp_path,
        id="test-id", title="Test",
        quest_dir=project_state_dir(tmp_path) / "quests" / "test-quest")


def _step(action, command: str = "run-gate") -> Step:
    return Step(
        name="verify", phase="p", kind="script",
        rules=(Rule(condition=None, action=action, mutations=(),
                    source=SourceLoc(file=Path("x"), line=1)),),
        on_max_retries=Pause(), max_loops=10,
        source=SourceLoc(file=Path("x"), line=1),
        command=command, interruptible=False)


def _policy(tmp_path: Path) -> SandboxPolicy:
    return SandboxPolicy(
        active=True, worktree=str(tmp_path), git_common_dir=str(tmp_path),
        git_ro_remask=(), writable_caches=(), extra_readable=(), extra_writable=(),
        secret_dirs=(), secret_files=(), child_env=(), network=True, note="")


def _run(step, quest, policy, log):
    async def main():
        state = SimpleNamespace(vars={}, route_label=None, loop_counts={})
        return await _executor._route_script(
            step, quest, state, log, sandbox_policy=policy)
    return anyio.run(main)


def _warn_msgs(log):
    return [e["payload"]["message"] for e in log.read()
            if e["type"] == "runner_warning"]


def _failing_open(monkeypatch) -> None:
    """Every keeper open fails, with no backoff. Retry is owned by open(), so the
    executor sees exactly one SandboxOpenFailed after the full budget of on_retry
    calls."""
    import imp.engine.sandbox as sandbox_mod

    def boom(cls, **kw):
        raise SandboxOpenFailed(_BWRAP_ERR)

    monkeypatch.setattr(SandboxNamespace, "_open_once", classmethod(boom))
    monkeypatch.setattr(sandbox_mod, "sandbox_spawn_backoff_delay", lambda a: 0.0)


def test_gate_retry_breadcrumbs_render_one_warning_per_failed_attempt(
        tmp_path, monkeypatch):
    # open() owns the budget and calls back per failed attempt (pinned in
    # tests/test_sandbox.py); the executor's job is to render each callback as its
    # own runner_warning, in order, with the step's own wording.
    def fake_run_script(step, **kw):
        kw["on_retry"](2, "first-boom")
        kw["on_retry"](3, "second-boom")
        return {"exit": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(_executor, "run_script", fake_run_script)
    log = InMemoryEventLog()
    action, _fb, _m = _run(_step(Continue()), _quest(tmp_path),
                           SimpleNamespace(active=True), log)
    assert isinstance(action, Continue)             # routed on the real exit 0
    assert _warn_msgs(log) == [
        "sandbox could not start the step; retrying (2/5)",
        "sandbox could not start the step; retrying (3/5)",
    ]
    payloads = [e["payload"] for e in log.read() if e["type"] == "runner_warning"]
    assert [p["detail"] for p in payloads] == ["first-boom", "second-boom"]
    assert all(p["reason"] == "sandbox-spawn-failed" for p in payloads)
    assert all(p["step"] == "verify" for p in payloads)


def test_gate_persistent_open_failure_pauses_without_routing(tmp_path, monkeypatch):
    _failing_open(monkeypatch)
    ruled = {"n": 0}
    real_eval = _executor.evaluate_rules
    monkeypatch.setattr(
        _executor, "evaluate_rules",
        lambda *a, **k: (ruled.__setitem__("n", ruled["n"] + 1)
                         or real_eval(*a, **k)))
    log = InMemoryEventLog()
    action, _fb, _m = _run(_step(Continue()), _quest(tmp_path),
                           _policy(tmp_path), log)
    assert isinstance(action, Pause)
    assert action.reason == "sandbox_spawn_failed"
    assert ruled["n"] == 0                          # evaluate_rules NEVER called
    assert "step_completed" not in [e["type"] for e in log.read()]
    assert len(_warn_msgs(log)) == SANDBOX_SPAWN_MAX_ATTEMPTS - 1


def test_gate_open_failure_never_runs_the_command(tmp_path, monkeypatch):
    # AC 16c, through the REAL gates.run_command: exhaustion pauses and the marker
    # file the gate command would have created is absent.
    _failing_open(monkeypatch)
    marker = tmp_path / "ran"
    log = InMemoryEventLog()
    action, _fb, _m = _run(_step(Continue(), command=f"touch {marker}"),
                           _quest(tmp_path), _policy(tmp_path), log)
    assert isinstance(action, Pause) and action.reason == "sandbox_spawn_failed"
    assert not marker.exists()
    details = [e["payload"]["detail"] for e in log.read()
               if e["type"] == "runner_warning"]
    assert details == [_BWRAP_ERR] * (SANDBOX_SPAWN_MAX_ATTEMPTS - 1)


@pytest.mark.parametrize("policy,exit_code,action,expected", [
    (SimpleNamespace(active=True), 1, Goto(target="rework"), Goto),
    (None, 0, Continue(), Continue),
], ids=["sandboxed", "bare"])
def test_a_real_gate_result_routes_on_its_exit_code_and_never_retries(
        tmp_path, monkeypatch, policy, exit_code, action, expected):
    # With the sentinel detection gone, a gate's exit code is the ONLY signal there
    # is: a healthy failure is a verdict, and marker-free stderr is just stderr.
    # Sandboxed and bare shapes collapse to one guarantee, so they are one test.
    monkeypatch.setattr(
        _executor, "run_script",
        lambda step, **kw: {"exit": exit_code, "stdout": "",
                            "stderr": "boom-detail"})
    log = InMemoryEventLog()
    got, feedback, _m = _run(_step(action), _quest(tmp_path), policy, log)
    assert isinstance(got, expected)
    assert _warn_msgs(log) == []                    # no retry breadcrumbs at all
    if expected is Goto:
        assert "boom-detail" in feedback            # the real stderr reached the agent
