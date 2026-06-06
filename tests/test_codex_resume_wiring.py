"""Fix 1 gating test (spec T0). RED until Task 5 wires codex-native resume.

The spike `docs/superpowers/spikes/codex/spike_codex_rollout_restore.py` proves
that restoring codex's rollout + calling `resume_thread(thread_id)` recalls real
prior context. This unit test guards the wiring that invokes that mechanism: on
a resume edge (`balance_on_resume=True`) the runner MUST call `resume_thread`
with the persisted thread_id and MUST NOT call `start_thread`. It runs in
`make test`, so its red CI blocks any Fix-2-only PR opened before Fix 1 lands.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext
from imp.engine.state import InMemoryEventLog
from imp.engine.types import (
    AgentDef,
    FsRule,
    OutputField,
    OutputSchema,
    SourceLoc,
)
from imp.providers.codex_runner import CodexAgentRunner
from tests.codex_fakes import (
    FakeCodex,
    agent_message,
    mcp_call,
    thread_started,
    turn_completed,
)

_SRC = SourceLoc(file=Path("a.md"), line=1)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _git_init(p: Path) -> None:
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.name", "T"], check=True)
    (p / ".gitkeep").write_text("")
    subprocess.run(["git", "-C", str(p), "add", ".gitkeep"], check=True)
    subprocess.run(["git", "-C", str(p), "commit", "-q", "-m", "init"], check=True)


def _seed_git_and_auth(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "auth.json").write_text('{"auth_mode": "chatgpt", "tokens": {}}')
    (tmp_path / "tx").mkdir(exist_ok=True)


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t", description="",
        tools=frozenset({"complete_step"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="answer", type="string", required=True),),
            source=_SRC),
        prompt_template_source="", source_path=Path("a.md"),
        source=_SRC, tier=None,
    )


def _build_invocation(
    tmp_path: Path, *, event_log: object, balance_on_resume: bool,
) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(), provider_name="codex", model_name="gpt-5.5",
        system_prompt="you are a test agent", initial_messages=(),
        change_context=ChangeContext(
            slug="s", title="t", project_root=tmp_path,
            workflow_dir=tmp_path, id="c1"),
        interactive=False, transcript_dir=tmp_path / "tx",
        fs_permissions=(
            FsRule(operations=("read", "write"), paths=("**",),
                   mode="allow", source=_SRC),),
        events_path=tmp_path / "events.jsonl", agent_id="step:t",
        event_log=event_log, balance_on_resume=balance_on_resume,
    )


@pytest.mark.anyio
async def test_resume_calls_resume_thread_not_start_thread(tmp_path: Path) -> None:
    _seed_git_and_auth(tmp_path)
    # Pre-seed transcript_dir as if a prior paused run persisted its rollout.
    tx = tmp_path / "tx"
    rel = "sessions/2026/06/05/rollout-2026-06-05T00-00-00-thr-resume-1.jsonl"
    (tx / "codex_thread.json").write_text(
        json.dumps({"thread_id": "thr-resume-1", "rollout_relpath": rel}))
    saved = tx / "codex_rollout" / rel
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text('{"id": "thr-resume-1"}\n')

    elog = InMemoryEventLog()
    scripted = [[
        thread_started("thr-resume-1"),
        agent_message("resumed; calling complete_step"),
        mcp_call(name="complete_step", args={"answer": "42"},
                 result="ok", item_id="cs-1"),
        turn_completed(input_tokens=1, output_tokens=1),
    ]]
    fake = FakeCodex(scripted_turns=scripted)
    runner = CodexAgentRunner(
        model_name="gpt-5.5", client_factory=lambda options=None: fake)
    runner._canonical_auth_override = tmp_path / "auth.json"

    inv = _build_invocation(tmp_path, event_log=elog, balance_on_resume=True)
    async for _ev in runner.run(inv):
        pass

    assert fake.resume_thread_calls, "resume must call client.resume_thread()"
    assert fake.resume_thread_calls[0][0] == "thr-resume-1"
    assert fake.start_thread_calls == [], "resume must NOT call start_thread()"


@pytest.mark.anyio
async def test_fresh_run_persists_rollout_to_transcript_dir(tmp_path: Path) -> None:
    """The persist call wired into run()'s finally must not break a normal
    (non-resumed) codex step. The fake client writes no real CODEX_HOME rollout,
    so persist_rollout no-ops here — this only guards that the wiring is harmless
    and teardown still runs. The real persist->restore round-trip is covered by
    tests/test_codex_rollout.py and the live dogfood (Task 9)."""
    _seed_git_and_auth(tmp_path)
    elog = InMemoryEventLog()

    scripted = [[
        thread_started("thr-persist-1"),
        agent_message("done"),
        mcp_call(name="complete_step", args={"answer": "x"},
                 result="ok", item_id="cs-1"),
        turn_completed(input_tokens=1, output_tokens=1),
    ]]
    fake = FakeCodex(scripted_turns=scripted)
    runner = CodexAgentRunner(
        model_name="gpt-5.5", client_factory=lambda options=None: fake)
    runner._canonical_auth_override = tmp_path / "auth.json"
    inv = _build_invocation(tmp_path, event_log=elog, balance_on_resume=False)

    evs = [ev async for ev in runner.run(inv)]

    # thread_id was captured from thread.started and (when a rollout exists)
    # persisted. With the fake client no real rollout is written, so persist is a
    # no-op — assert it did NOT crash the run and teardown still completed.
    # `agent_output` is a YIELDED event (codex_runner.py:516), never written to
    # the event log, so assert on the yielded stream, not elog.read().
    assert any(e.get("type") == "agent_output" for e in evs), \
        "run completed cleanly with persist wired into finally"
