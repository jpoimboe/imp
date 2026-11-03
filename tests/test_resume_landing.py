"""Daemon-level landing: a leave records where resume lands from where the user was."""
from __future__ import annotations

import contextlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.engine.executor import RunResult, run_quest
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.paths import project_state_dir
from imp.remote.session import daemon
from imp.remote.session.client import SessionClient
from imp.remote.session.daemon import serve_quest_session
from imp.remote.session.discover import session_socket_path
from imp.remote.session.protocol import CONTROL_INTERRUPT, CONTROL_INTERRUPT_LEAVE
from tests.async_wait import HANG_BACKSTOP, wait_until
from tests.conftest import _git
from tests.test_session_daemon import _make_quest, _seed_step, _wait_for_socket

pytestmark = pytest.mark.timeout(240)

QID = "2026-11-03-landing"
Events = list[dict[str, Any]]
Body = Callable[[SessionClient, Events], Awaitable[None]]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    from imp.providers import register_builtin_providers
    register_builtin_providers()
    monkeypatch.setattr(daemon, "install_stack_dumper", lambda p: None)
    monkeypatch.setattr("imp.cli.signals._COOPERATIVE_DEADLINE_S", 0.3)


def _busy(marker: Path) -> dict[str, Any]:
    # Sleeps only on the first run, so a replayed step gets past it.
    cmd = f"echo x >> {marker}; [ $(wc -l < {marker}) -ge 2 ] || sleep 30"
    return {"text": "working", "tools": [{"name": "bash", "input": {"command": cmd}}]}


_DONE = {"text": "done", "tools": [
    {"name": "complete_step", "input": {"outcome": "ok", "status": "Success"}}]}


def _seed_gate_quest(
    root: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
    turns: list[dict[str, Any]],
) -> Quest:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n.imp-isolated/\nscript.json\nmarker\n")
    wf = root / "imp" / "workflows" / "default"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "gate.md").write_text(
        "---\nname: gate\ndescription: g\ntools: [bash, complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\nwork\n")
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: \"\"\nphases:\n  - name: work\n    steps:\n"
        "      - name: gate\n        file: agents/gate.md\n        tier: low\n"
        "        interactive: true\n")
    (root / "imp" / "config.yaml").write_text(
        "default-workflow: default\ndefault-base: main\n")
    (project_state_dir(root) / "config.yaml").write_text(
        "providers: [scripted]\ntiers:\n  low: {scripted: x}\n  medium: {scripted: x}\n"
        "  high: {scripted: x}\nsandbox:\n  enabled: false\n")
    script = root / "script.json"
    script.write_text(json.dumps({"interrupt": mode, "default": turns}))
    monkeypatch.setenv("IMP_SCRIPTED_SCRIPT", str(script))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup")
    return Quest.create(root, title="x", quest_id=QID, base_branch="main")


async def _real_run(q: Quest, **kw: Any) -> Any:
    # A run hitting no gate before the leave never awaits the first attach itself.
    await wait_until(lambda: "cli_attached" in q.events_path.read_text())
    return await run_quest(q, **kw)


async def _leg(
    quest: Quest, run_fn: Any, body: Body | None = None, *,
    resume: bool = False, first_attach_timeout: float = 30.0,
) -> Any:
    """Serve one daemon session to its end, driving one leased client with `body`."""
    out: dict[str, Any] = {}
    with anyio.fail_after(HANG_BACKSTOP):
        async with anyio.create_task_group() as tg:
            async def _serve() -> None:
                out["r"] = await serve_quest_session(
                    quest, run_fn=run_fn, resume=resume, hibernate=True,
                    first_attach_timeout=first_attach_timeout)
                tg.cancel_scope.cancel()

            tg.start_soon(_serve)
            if body is not None:
                sock = str(session_socket_path(quest.id))
                await _wait_for_socket(sock)
                received: Events = []
                client = SessionClient(sock, quest.id, quest.title,
                                       on_event=received.append, lease=True)
                await client.connect()
                tg.start_soon(client.receive_events)
                try:
                    await body(client, received)
                finally:
                    with contextlib.suppress(Exception):
                        await client.aclose()
            await anyio.sleep_forever()
    return out["r"]


async def _leave(client: SessionClient, gesture: str) -> None:
    if gesture != "lease-drop":
        await client.send_control(CONTROL_INTERRUPT_LEAVE, gesture)
    await client.aclose()   # with no frame: the terminal vanished


def _landing(quest: Quest) -> tuple[str | None, str | None, bool]:
    st = State.load(quest.state_dir)
    return st.resume_pending, st.resume_target, st.resume_should_continue


def _journal(quest: Quest) -> Events:
    return [json.loads(ln) for ln in quest.events_path.read_text().splitlines()]


def _lines(p: Path) -> int:
    return len(p.read_text().splitlines()) if p.exists() else 0


def _gates(received: Events) -> Events:
    """Live gates only: an attach first replays the journal, flagged `_replay`."""
    return [e for e in received
            if e.get("type") == "awaiting_user_input" and not e.get("_replay")]


def _fake_recorder() -> tuple[list[int], Any]:
    calls: list[int] = []

    async def _fake(_q: Quest, **_kw: Any) -> RunResult:
        calls.append(1)
        return RunResult.PAUSED

    return calls, _fake


_MODES = ["cooperative", "hard_kill"]


@pytest.mark.anyio
@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("gesture", ["ctrl-d", "sigterm", "sighup", "sigquit", "lease-drop"])
async def test_midrun_leave_resumes_into_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gesture: str, mode: str,
) -> None:
    marker = tmp_path / "marker"
    quest = _seed_gate_quest(tmp_path, monkeypatch, mode, [_busy(marker), _DONE])

    async def _body(client: SessionClient, _r: Events) -> None:
        await wait_until(lambda: _lines(marker) == 1)
        await _leave(client, gesture)

    assert await _leg(quest, _real_run, _body) is RunResult.PAUSED
    pending, target, cont = _landing(quest)
    assert (pending, cont) == ("gate", True)
    assert target in (None, "agent")
    if mode == "cooperative":   # the late inline steer gate fired but did not win
        assert any(e["type"] == "awaiting_user_input" and e["payload"].get("interrupt")
                   for e in _journal(quest))

    calls, fake = _fake_recorder()
    await _leg(quest, fake, resume=True, first_attach_timeout=0.1)
    assert calls == [1]


def _interrupt_gate(received: Events) -> bool:
    return any(g["payload"].get("interrupt") for g in _gates(received))


@pytest.mark.anyio
@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("leave", ["ctrl-d", "sigterm", "lease-drop"])
async def test_steer_gate_leave_resumes_at_the_interrupted_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, leave: str, mode: str,
) -> None:
    marker = tmp_path / "marker"
    quest = _seed_gate_quest(tmp_path, monkeypatch, mode, [_busy(marker), _DONE])

    async def _body(client: SessionClient, received: Events) -> None:
        await wait_until(lambda: _lines(marker) == 1)
        await client.send_control(CONTROL_INTERRUPT)
        await wait_until(lambda: bool(_interrupt_gate(received)))
        await _leave(client, leave)

    assert await _leg(quest, _real_run, _body) is RunResult.PAUSED
    assert _landing(quest) == ("gate", "prompt-interrupted", True)

    calls, fake = _fake_recorder()
    seen: Events = []

    async def _resumed(client: SessionClient, received: Events) -> None:
        await wait_until(lambda: bool(_gates(received)))
        seen.extend(received)
        await _leave(client, "ctrl-d")

    await _leg(quest, fake, _resumed, resume=True)
    assert _gates(seen)[0]["payload"]["steer_hint"] == daemon._RESUME_INTERRUPTED_PROMPT
    assert calls == []


_HOLD_CASES = [
    pytest.param("prompt-interrupted", True, "threshold",
                 daemon._RESUME_INTERRUPTED_PROMPT, id="interrupted"),
    pytest.param("prompt-paused", False, "threshold", daemon._RESUME_PROMPT, id="paused"),
    pytest.param("prompt-paused", False, "verify", daemon._SCRIPT_HOLD_PROMPT, id="script"),
]


@pytest.mark.anyio
@pytest.mark.parametrize(("target", "cont", "step", "hint"), _HOLD_CASES)
@pytest.mark.parametrize("leave", ["ctrl-d", "pause", "lease-drop"])
async def test_leave_at_a_resumed_hold_prompt_lands_there_again(
    tmp_path: Path, leave: str, target: Any, cont: bool, step: str, hint: str,
) -> None:
    # No config: the bundled default workflow, where threshold is an agent and verify a script.
    quest = _make_quest(tmp_path, QID)
    st = State.load(quest.state_dir)
    st.step = step
    st.arm_step_resume(should_continue=cont, target=target)
    st.save(quest.state_dir)
    calls, fake = _fake_recorder()

    async def _body(client: SessionClient, received: Events) -> None:
        await wait_until(lambda: bool(_gates(received)))
        assert _gates(received)[0]["payload"]["steer_hint"] == hint
        await _leave(client, leave)

    assert await _leg(quest, fake, _body, resume=True) is RunResult.PAUSED
    assert _landing(quest) == (step, target, cont)
    assert calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("gesture", ["ctrl-d", "sigterm"])
async def test_midrun_leave_on_a_script_step_reruns_the_gate(
    tmp_path: Path, gesture: str,
) -> None:
    quest = _make_quest(tmp_path, QID)
    _seed_step(quest, "verify")
    st = State.load(quest.state_dir)
    st.arm_step_resume(should_continue=False, target="prompt-paused")   # stale: leave must clear
    st.save(quest.state_dir)
    started: list[int] = []

    async def _script_step(_q: Quest, *, coordinator: Any, **_kw: Any) -> RunResult:
        started.append(1)
        await wait_until(lambda: coordinator.should_pause)
        return RunResult.PAUSED   # the executor arms no resume for a script step

    async def _body(client: SessionClient, _r: Events) -> None:
        await wait_until(lambda: bool(started))
        await _leave(client, gesture)

    assert await _leg(quest, _script_step, _body) is RunResult.PAUSED
    pending, target, _ = _landing(quest)
    assert (pending, target) == (None, None)

    calls, fake = _fake_recorder()   # a wrongly shown hold prompt hibernates instead
    await _leg(quest, fake, resume=True, first_attach_timeout=0.1)
    assert calls == [1]


async def _leave_at_the_steer_gate(quest: Quest, marker: Path) -> None:
    async def _body(client: SessionClient, received: Events) -> None:
        await wait_until(lambda: _lines(marker) == 1)
        await client.send_control(CONTROL_INTERRUPT)
        await wait_until(lambda: bool(_interrupt_gate(received)))
        await _leave(client, "ctrl-d")

    await _leg(quest, _real_run, _body)
    assert _landing(quest) == ("gate", "prompt-interrupted", True)


@pytest.mark.anyio
async def test_steer_landing_does_not_outlive_a_later_midrun_leave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "marker"
    always_busy = {"text": "working", "tools": [
        {"name": "bash", "input": {"command": f"echo x >> {marker}; sleep 30"}}]}
    quest = _seed_gate_quest(tmp_path, monkeypatch, "cooperative", [always_busy, _DONE])
    await _leave_at_the_steer_gate(quest, marker)

    async def _resumed(client: SessionClient, received: Events) -> None:
        await wait_until(lambda: bool(_gates(received)))
        assert _gates(received)[0]["payload"]["steer_hint"] == (
            daemon._RESUME_INTERRUPTED_PROMPT)
        await client.send_input("")
        await wait_until(lambda: _lines(marker) == 2)
        await _leave(client, "ctrl-d")

    assert await _leg(quest, _real_run, _resumed, resume=True) is RunResult.PAUSED
    pending, target, cont = _landing(quest)
    assert (pending, cont) == ("gate", True)
    assert target in (None, "agent")


@pytest.mark.anyio
@pytest.mark.parametrize("leave", ["ctrl-d", "lease-drop"])
async def test_answer_at_a_reasked_question_reaches_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, leave: str,
) -> None:
    marker = tmp_path / "marker"
    quest = _seed_gate_quest(tmp_path, monkeypatch, "cooperative", [
        _busy(marker), {"text": "which way?"}, {"text": "anything else?"}, _DONE])
    await _leave_at_the_steer_gate(quest, marker)

    async def _asked(client: SessionClient, received: Events) -> None:
        await wait_until(lambda: bool(_gates(received)))
        await client.send_input("")
        await wait_until(lambda: len(_gates(received)) == 2)
        assert _gates(received)[1]["payload"]["interrupt"] is False
        await _leave(client, leave)

    assert await _leg(quest, _real_run, _asked, resume=True) is RunResult.PAUSED
    assert _landing(quest) == ("gate", None, False)

    def _answered() -> bool:
        return any(e["type"] == "user_message" and "MY-ANSWER" in json.dumps(e["payload"])
                   for e in _journal(quest))

    async def _answer(client: SessionClient, received: Events) -> None:
        await wait_until(lambda: bool(_gates(received)))
        first = _gates(received)[0]["payload"]
        assert (first["interrupt"], first["last_agent_message"]) == (False, "which way?")
        await client.send_input("MY-ANSWER")
        await wait_until(_answered)
        await _leave(client, "ctrl-d")

    await _leg(quest, _real_run, _answer, resume=True)
    assert _lines(marker) == 3   # the agent replayed into the re-asked question
