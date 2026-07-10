"""Regression: once resume_pending is armed on an interactive bare-turn-end leave
(as abandon.py now arms it), `imp resume` RESUMES — it keeps the transcript and
native session and does not re-deliver the seed. The inverse of the spike's
test_resume_pending_none scenario. Also guards that an unarmed entry and an
armed-but-empty-transcript entry still start fresh (seed re-delivered)."""
from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event, Message, TextBlock
from imp.agents.transcript import Transcript
from imp.engine.change import Change
from imp.engine.executor import run_change
from imp.engine.state import State

SEED = "help me design a caching layer"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _seed_interactive_change(root: Path, *, armed: bool, started: bool = True):
    """Build a paused interactive one-step change whose entry step is `chat`.
    `armed` sets resume_pending to the step (the post-leave armed value) or None."""
    from imp.engine.loader import load_workflow
    from imp.providers.effective import load_effective_config
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "chat.md").write_text(
        "---\nname: chat\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\n"
        "brainstorm partner. opening: {{ run.opening }}\n")
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 3, max_loops: 6}\n"
        "phases:\n  - name: only\n    steps:\n"
        "      - {name: chat, file: agents/chat.md, tier: low, interactive: true,\n"
        "         rules: [{else: true, continue: true}]}\n")
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(
        "providers: [fake]\ntiers:\n"
        "  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n")
    quest_dir = root / ".imp" / "change"
    (quest_dir / "state").mkdir(parents=True, exist_ok=True)
    (quest_dir / "quest.json").write_text(json.dumps(
        {"id": "c", "title": "t", "base_branch": "main", "seed_opening": SEED},
        sort_keys=True))
    configured: list[str] = []
    with contextlib.suppress(Exception):
        configured = load_effective_config(
            project_root=root, control_root=root).config.tier_names()
    workflow = load_workflow(wf, configured_tiers=configured)
    flat = workflow.flat_steps()
    st = State(id="c", step=flat[0].name, step_attempt=0,
               status="paused" if started else "new",
               vars=dict(workflow.vars.defaults()), loop_counts={})
    st.has_started = started
    st.resume_pending = flat[0].name if armed else None
    st.save(quest_dir / "state")
    change = Change(id="c", title="t", base_branch="main",
                    control_root=root, project_root=root, quest_dir=quest_dir,
                    seed_opening=SEED)
    change.event_log().append(agent_id=None, type="change_created",
                              payload={"id": "c", "title": "t"})
    return change, flat[0].name


def _write_awaiting_user_transcript(change: Change, step_name: str) -> Path:
    """A non-empty AWAITING_USER transcript + a native claude_session store under it,
    plus the stored step_context the armed resume path replays."""
    tdir = change.quest_dir / "steps" / "only" / step_name
    tdir.mkdir(parents=True, exist_ok=True)
    t = Transcript(tdir / "transcript.jsonl")
    t.append(Message(role="user", content=(
        TextBlock(text=f"brainstorm partner. opening: {SEED}"),)))
    t.append(Message(role="assistant", content=(
        TextBlock(text="Sure! What's your read/write ratio?"),)))
    (tdir / "claude_session").mkdir(parents=True, exist_ok=True)
    (tdir / "claude_session" / "sentinel").write_text("native session bytes")
    # On the armed resume path (is_resume=True) the executor replays the stored
    # step_context system prompt; a non-empty transcript with NO stored prompt
    # trips the resume-invariant guard (executor.py:_resume_system_prompt) and
    # raises before the provider runs. Seed the step_context the first run emitted.
    change.event_log().append_step_context(
        agent_id=f"step:only/{step_name}", provider="fake", model="m",
        system_prompt=f"brainstorm partner. opening: {SEED}",
        tools_schema=[], params={}, max_tool_calls=None)
    return tdir


def _register_observing_provider(provider_registry, tdir: Path, observed: dict) -> None:
    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            im_text = " ".join(
                b.text for m in inv.initial_messages for b in m.content
                if isinstance(b, TextBlock))
            observed["seed_in_initial"] = "caching layer" in im_text
            observed["seed_count"] = im_text.count("caching layer")
            observed["transcript_nonempty_at_run"] = (
                Transcript(tdir / "transcript.jsonl").exists_and_nonempty())
            observed["native_store_present_at_run"] = (
                tdir / "claude_session" / "sentinel").exists()
            yield {"type": "agent_output",
                   "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"

        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()

    provider_registry("fake", env_check=lambda: True, factory=lambda _m: _P())


@pytest.mark.anyio
async def test_armed_resume_keeps_transcript_native_and_no_seed(
        tmp_path: Path, provider_registry) -> None:
    # Armed marker + non-empty AWAITING_USER transcript => is_resume and keep both True.
    change, step_name = _seed_interactive_change(tmp_path, armed=True)
    tdir = _write_awaiting_user_transcript(change, step_name)
    observed: dict = {}
    _register_observing_provider(provider_registry, tdir, observed)

    await run_change(change)

    # keep=True: the prior transcript survived into the run (not wiped)...
    assert observed["transcript_nonempty_at_run"] is True
    # ...the native claude_session store under it survived...
    assert observed["native_store_present_at_run"] is True
    # ...and the seed was NOT re-delivered as a fresh opening.
    assert observed["seed_in_initial"] is False
    # AC3(a)/(b) made explicit: the entry step re-rendered as a resume, not a
    # restart — the step_started event carries resumed=True.
    events = [json.loads(ln) for ln in
              change.events_path.read_text().splitlines() if ln.strip()]
    starts = [e for e in events if e["type"] == "step_started"]
    assert any(
        e["payload"].get("step") == step_name and e["payload"].get("resumed") is True
        for e in starts
    )


@pytest.mark.anyio
async def test_fresh_run_delivers_seed_once(
        tmp_path: Path, provider_registry) -> None:
    # Unarmed (resume_pending None), never started, no prior transcript: a genuine
    # first run => is_resume False, seed delivered exactly once.
    change, step_name = _seed_interactive_change(tmp_path, armed=False, started=False)
    tdir = change.quest_dir / "steps" / "only" / step_name
    observed: dict = {}
    _register_observing_provider(provider_registry, tdir, observed)

    await run_change(change)

    assert observed["seed_in_initial"] is True
    assert observed["seed_count"] == 1
    assert observed["transcript_nonempty_at_run"] is False


@pytest.mark.anyio
async def test_armed_but_empty_transcript_starts_fresh(
        tmp_path: Path, provider_registry) -> None:
    # Armed marker but NO transcript (draft that never produced a turn): keep gates
    # on a non-empty transcript, so it still starts fresh and re-delivers the seed.
    change, step_name = _seed_interactive_change(tmp_path, armed=True)
    tdir = change.quest_dir / "steps" / "only" / step_name
    observed: dict = {}
    _register_observing_provider(provider_registry, tdir, observed)

    await run_change(change)

    assert observed["seed_in_initial"] is True
    assert observed["transcript_nonempty_at_run"] is False
