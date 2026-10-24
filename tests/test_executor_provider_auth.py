from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from imp.engine import executor
from imp.engine.env import provider_auth_dir
from imp.engine.executor import RunResult, run_quest
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.engine.types import Continue, Goto, Pause
from imp.paths import project_state_dir
from imp.providers import provider_auth
from tests.test_executor import _FAKE_CONFIG, _make_quest, _register_fake, _write_fake_config


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _seed(root: Path, *, slow_timeout: int | None = None) -> Quest:
    wf = root / "imp" / "workflows" / "imp"
    wf.mkdir(parents=True)
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - name: s1\n"
        "        command: 'true'\n"
        "        rules:\n"
        "          - {else: true, continue: true}\n")
    _write_fake_config(root)
    if slow_timeout is not None:
        (project_state_dir(root) / "config.yaml").write_text(
            _FAKE_CONFIG + f"test_cmd_slow_timeout: {slow_timeout}\n")
    _register_fake({"status": "Success"})
    return _make_quest(root)


def _sandbox(monkeypatch: pytest.MonkeyPatch, *, active: bool) -> None:
    monkeypatch.setattr(executor, "_build_sandbox_policy",
                        lambda quest, config, log: SimpleNamespace(active=active))


def _record_checks(monkeypatch: pytest.MonkeyPatch, trace: list[str], *,
                   messages: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _check(**kw: Any) -> list[str]:
        trace.append("check")
        calls.append({**kw, "thread": threading.get_ident()})
        return list(messages)

    monkeypatch.setattr(executor, "refresh_and_stage", _check)
    return calls


def _dispatch_returning(monkeypatch: pytest.MonkeyPatch, trace: list[str],
                        actions: list[Any],
                        resumes: list[Any] | None = None) -> list[int]:
    loop_threads: list[int] = []

    async def _fake(step: Any, workflow: Any, quest_: Any, state: Any, log: Any,
                    **kw: Any) -> tuple[Any, None, dict[str, Any]]:
        trace.append("dispatch")
        loop_threads.append(threading.get_ident())
        if resumes is not None:
            resumes.append(kw.get("is_resume"))
        return (actions.pop(0), None, {})

    monkeypatch.setattr(executor, "_dispatch_step", _fake)
    return loop_threads


@pytest.mark.anyio
async def test_the_check_precedes_every_sandboxed_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _seed(tmp_path)
    _sandbox(monkeypatch, active=True)
    trace: list[str] = []
    _record_checks(monkeypatch, trace)
    # first step, same-step re-dispatch, then a pause and a resume in a new run
    resumes: list[Any] = []
    _dispatch_returning(monkeypatch, trace, [Goto("s1"), Pause(), Continue()], resumes)

    assert await run_quest(quest, on_event=lambda _e: None) is RunResult.PAUSED
    assert trace == ["check", "dispatch", "check", "dispatch"]
    parked = State.load(quest.state_dir)
    parked.arm_step_resume(should_continue=True)
    parked.save(quest.state_dir)
    assert await run_quest(quest, on_event=lambda _e: None) is RunResult.COMPLETED
    assert trace == ["check", "dispatch"] * 3
    assert resumes == [False, False, True]


@pytest.mark.anyio
async def test_no_check_runs_without_an_active_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _seed(tmp_path)
    _sandbox(monkeypatch, active=False)
    trace: list[str] = []
    _record_checks(monkeypatch, trace)
    _dispatch_returning(monkeypatch, trace, [Continue()])

    assert await run_quest(quest, on_event=lambda _e: None) is RunResult.COMPLETED
    assert trace == ["dispatch"]


@pytest.mark.anyio
async def test_the_check_runs_off_the_event_loop_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _seed(tmp_path)
    _sandbox(monkeypatch, active=True)
    trace: list[str] = []
    calls = _record_checks(monkeypatch, trace)
    loop_threads = _dispatch_returning(monkeypatch, trace, [Continue()])

    assert await run_quest(quest, on_event=lambda _e: None) is RunResult.COMPLETED
    assert calls[0]["thread"] != loop_threads[0]


@pytest.mark.anyio
@pytest.mark.parametrize(("slow_timeout", "window_ms"), [(None, 600_000), (3600, 3_900_000)])
async def test_the_window_is_the_slow_gate_timeout_plus_five_minutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    slow_timeout: int | None, window_ms: int,
) -> None:
    quest = _seed(tmp_path, slow_timeout=slow_timeout)
    _sandbox(monkeypatch, active=True)
    trace: list[str] = []
    calls = _record_checks(monkeypatch, trace)
    _dispatch_returning(monkeypatch, trace, [Continue()])

    assert await run_quest(quest, on_event=lambda _e: None) is RunResult.COMPLETED
    assert calls[0]["window_ms"] == window_ms
    assert calls[0]["target"] == provider_auth_dir()


@pytest.mark.anyio
async def test_check_warnings_surface_and_the_step_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _seed(tmp_path)
    _sandbox(monkeypatch, active=True)
    trace: list[str] = []
    _record_checks(monkeypatch, trace, messages=("first problem", "second problem"))
    _dispatch_returning(monkeypatch, trace, [Pause()])

    assert await run_quest(quest, on_event=lambda _e: None) is RunResult.PAUSED
    assert trace == ["check", "dispatch"]
    warned = [e["payload"]["message"] for e in quest.event_log().read()
              if e["type"] == "runner_warning"]
    assert warned == ["first problem", "second problem"]


@pytest.mark.anyio
async def test_the_real_check_stages_the_copy_before_the_first_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _seed(tmp_path)
    _sandbox(monkeypatch, active=True)
    claude = tmp_path / "canon-claude"
    claude.mkdir()
    (claude / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "host-at", "refreshToken": "host-rt"}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setattr(executor, "refresh_and_stage", provider_auth.refresh_and_stage)
    seen: list[Any] = []

    async def _fake(step: Any, workflow: Any, quest_: Any, state: Any, log: Any,
                    **kw: Any) -> tuple[Any, None, dict[str, Any]]:
        seen.append(json.loads(
            (provider_auth_dir() / "claude" / ".credentials.json").read_text()))
        return (Continue(), None, {})

    monkeypatch.setattr(executor, "_dispatch_step", _fake)

    assert await run_quest(quest, on_event=lambda _e: None) is RunResult.COMPLETED
    assert seen == [{"claudeAiOauth": {"accessToken": "host-at"}}]
