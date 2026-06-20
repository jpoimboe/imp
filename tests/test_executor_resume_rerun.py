from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event, Message, TextBlock, ToolUseBlock
from imp.agents.transcript import Transcript
from imp.engine.change import Change
from imp.engine.executor import RunResult, _resume_replays_transcript, run_change
from imp.engine.state import State


def _write(dir_: Path, *messages: Message) -> None:
    t = Transcript(dir_ / "transcript.jsonl")
    for msg in messages:
        t.append(msg)


def test_truncated_autonomous_transcript_is_replayed(tmp_path: Path) -> None:
    # P1: a bare assistant turn (MISSING_COMPLETE_STEP) is now KEPT and replayed;
    # the provider injects a continue nudge so it proceeds instead of re-running.
    _write(tmp_path, Message(role="assistant",
                             content=(TextBlock(text="still working"),)))
    assert _resume_replays_transcript(tmp_path) is True


def test_pending_user_turn_is_replayed(tmp_path: Path) -> None:
    # Last message is a user turn → SEND_TO_LLM, safe to replay.
    _write(tmp_path, Message(role="user", content=(TextBlock(text="hi"),)))
    assert _resume_replays_transcript(tmp_path) is True


def test_awaiting_user_is_replayed(tmp_path: Path) -> None:
    # Dangling wait_for_user → AWAITING_USER, replay resolves it with the reply.
    _write(tmp_path, Message(
        role="assistant",
        content=(ToolUseBlock(id="w1", name="wait_for_user", input={}),)))
    assert _resume_replays_transcript(tmp_path) is True


def test_empty_transcript_is_not_replayed(tmp_path: Path) -> None:
    # Nothing on disk: there is nothing to replay. keep=True would route the
    # step through the resume branch (initial_messages=()), and the provider's
    # empty-transcript fallback would then build an EMPTY query that hangs
    # receive_response() forever. An empty transcript must run fresh.
    assert _resume_replays_transcript(tmp_path) is False


def test_dangling_tool_use_is_replayed(tmp_path: Path) -> None:
    # DANGLING_TOOL_USE: assistant turn with a non-wait_for_user tool use.
    _write(tmp_path, Message(
        role="assistant",
        content=(ToolUseBlock(id="t1", name="write_file", input={}),)))
    assert _resume_replays_transcript(tmp_path) is True


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _seed_single_agent(root: Path) -> Change:
    from imp.engine.loader import load_workflow
    from imp.providers.effective import load_effective_config
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n")
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 3, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n  - name: only\n    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: medium}\n")
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(
        "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n")
    change_dir = root / ".imp" / "change"
    (change_dir / "state").mkdir(parents=True, exist_ok=True)
    (change_dir / "change.json").write_text(json.dumps(
        {"id": "c", "title": "t", "base_branch": "main"}, sort_keys=True))
    configured: list[str] = []
    with contextlib.suppress(Exception):
        configured = load_effective_config(
            project_root=root, control_root=root).config.tier_names()
    workflow = load_workflow(wf, configured_tiers=configured)
    flat = workflow.flat_steps()
    State(id="c", step=flat[0].name, step_attempt=0, status="running",
          vars=dict(workflow.vars.defaults()), loop_counts={}).save(change_dir / "state")
    change = Change(id="c", title="t", base_branch="main",
                    control_root=root, project_root=root, change_dir=change_dir)
    change.event_log().append(agent_id=None, type="change_created",
                              payload={"id": "c", "title": "t"})
    return change


@pytest.mark.anyio
async def test_resume_truncated_autonomous_replays_and_advances(
        tmp_path: Path, provider_registry) -> None:
    change = _seed_single_agent(tmp_path)
    state = State.load(change.state_dir)
    state.resume_pending = state.step
    state.save(change.state_dir)
    tdir = change.change_dir / "steps" / "only" / "w"
    tdir.mkdir(parents=True, exist_ok=True)
    Transcript(tdir / "transcript.jsonl").append(
        Message(role="assistant", content=(TextBlock(text="still working"),)))
    # Seed the step_context event so _resume_system_prompt finds the stored
    # prompt (in production this is always present when a transcript exists).
    change.event_log().append(
        agent_id="step:only/w", type="step_context",
        payload={"system_prompt": "test"})

    seen_nonempty: list[bool] = []

    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            # P1: the executor KEEPS the transcript (replay-and-continue), so the
            # runner sees a non-empty transcript at entry.
            seen_nonempty.append(
                Transcript(tdir / "transcript.jsonl").exists_and_nonempty())
            yield {"type": "agent_output", "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()

    provider_registry("fake", env_check=lambda: True, factory=lambda _m: _P())

    result = await run_change(change)
    assert result is RunResult.COMPLETED          # advanced, not wedged
    assert seen_nonempty == [True]                # transcript kept and replayed
    assert State.load(change.state_dir).status == "completed"


@pytest.mark.anyio
async def test_resume_empty_transcript_seeds_fresh_and_advances(
        tmp_path: Path, provider_registry) -> None:
    # A `resume:` edge into a step that never ran (empty transcript). This is the
    # reintegration path: a clean rebase skips the integrator, integrate-verify
    # then fails, and its `resume: integrator` rule resumes a step with no
    # transcript. The buggy path kept the (empty) transcript and passed
    # initial_messages=(), so the provider built an empty query and hung. The fix
    # runs the step fresh: a non-empty seed reaches the runner and it advances.
    change = _seed_single_agent(tmp_path)
    state = State.load(change.state_dir)
    state.resume_pending = state.step
    state.save(change.state_dir)
    # Step's transcript dir exists but is empty — the never-ran case.
    tdir = change.change_dir / "steps" / "only" / "w"
    tdir.mkdir(parents=True, exist_ok=True)

    seen_initial_nonempty: list[bool] = []

    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            # The crux: a fresh re-run must hand the runner a non-empty seed so
            # the provider sends a real first query (no infinite receive_response).
            seen_initial_nonempty.append(len(inv.initial_messages) > 0)
            yield {"type": "agent_output", "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()

    provider_registry("fake", env_check=lambda: True, factory=lambda _m: _P())

    result = await run_change(change)
    assert result is RunResult.COMPLETED          # advanced, not wedged
    assert seen_initial_nonempty == [True]        # fresh seed, not an empty replay
    assert State.load(change.state_dir).status == "completed"
