from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.engine.quest import Quest
from imp.engine.executor import RunResult, run_change
from imp.engine.state import State


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_change(root: Path, *, quest_id: str = "c", title: str = "t") -> Quest:
    """Construct a Quest directly on disk without git worktrees."""
    from imp.engine.loader import load_workflow
    from imp.providers.effective import load_effective_config

    src_wf = root / "imp" / "workflows" / "imp"
    quest_dir = root / ".imp" / "change"
    quest_dir.mkdir(parents=True, exist_ok=True)
    (quest_dir / "state").mkdir(exist_ok=True)
    (quest_dir / "quest.json").write_text(json.dumps({
        "id": quest_id, "title": title, "base_branch": "main",
    }, indent=2, sort_keys=True))

    configured_tiers: list[str] = []
    with contextlib.suppress(Exception):
        configured_tiers = load_effective_config(
            project_root=root, control_root=root).config.tier_names()

    workflow = load_workflow(src_wf, configured_tiers=configured_tiers)
    flat = workflow.flat_steps()
    State(
        id=quest_id, step=flat[0].name if flat else None, step_attempt=0,
        status="running", vars=dict(workflow.vars.defaults()), loop_counts={},
    ).save(quest_dir / "state")

    change = Quest(
        id=quest_id, title=title, base_branch="main",
        control_root=root, project_root=root, quest_dir=quest_dir,
    )
    change.event_log().append(
        agent_id=None, type="change_created",
        payload={"id": quest_id, "title": title},
    )
    return change


def _seed_single_agent(root: Path) -> Quest:
    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "writer.md").write_text(
        "---\nname: writer\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
    )
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 3, max_loops: 6, on_max_retries: {pause: true}}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
    )
    # run_change reads via load_effective_config: project marker + local layer.
    (root / "imp").mkdir(parents=True, exist_ok=True)
    (root / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (root / ".imp").mkdir(parents=True, exist_ok=True)
    (root / ".imp" / "config.yaml").write_text(
        "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n"
    )
    return _make_change(root)


@pytest.mark.anyio
async def test_mid_turn_interrupt_sets_resume_pending(tmp_path: Path) -> None:
    change = _seed_single_agent(tmp_path)
    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    from imp.cli.signals import InterruptCoordinator
    coord = InterruptCoordinator()

    class _InterruptingRunner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            # Simulate a mid-turn Ctrl-C: flip should_pause, then yield ONE event
            # so drive_invocation's per-event should_pause check observes it and
            # returns paused (the clean cooperative interrupt).
            await coord.signal_pause(reason="ctrl-c")
            yield {"type": "agent_output", "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _inv: AgentInvocation) -> AgentRunner:
            return _InterruptingRunner()

    resolve.register_provider("fake", env_check=lambda: True, factory=lambda _m: _P())

    result = await run_change(change, coordinator=coord)
    assert result is RunResult.PAUSED
    state = State.load(change.state_dir)
    assert state.status == "paused"
    assert state.resume_pending == state.step  # transcript kept on re-entry


@pytest.mark.anyio
async def test_interrupt_after_step_completed_advances_then_pauses(tmp_path: Path) -> None:
    """Two-step workflow. Interrupt fires as step 1 completes (Continue). The
    cursor must advance to step 2, THEN pause — re-entry starts step 2 fresh."""
    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "a.md").write_text(
        "---\nname: a\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n")
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\ndefaults: {max_retries: 3, max_loops: 6}\n"
        "phases:\n  - name: only\n    steps:\n"
        "      - {name: s1, file: agents/a.md, tier: low}\n"
        "      - {name: s2, file: agents/a.md, tier: low}\n")
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (tmp_path / ".imp").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".imp" / "config.yaml").write_text(
        "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n")
    change = _make_change(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    from imp.cli.signals import InterruptCoordinator
    coord = InterruptCoordinator()

    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            # Complete cleanly (→ Continue). The interrupt is injected AFTER the
            # step completes, via the on_event hook below — NOT inside run().
            yield {"type": "agent_output", "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()

    resolve.register_provider("fake", env_check=lambda: True, factory=lambda _m: _P())

    def _on_event(e: dict) -> None:
        # Flip should_pause synchronously the instant step s1 completes — the
        # interrupt-after-completion window. on_event is sync, so set the flag
        # directly (signal_pause is async and can't be awaited here).
        if (e.get("type") == "step_completed"
                and (e.get("payload") or {}).get("step") == "s1"):
            coord._should_pause = True
            coord.pause_reason = "ctrl-c"

    result = await run_change(change, on_event=_on_event, coordinator=coord)
    assert result is RunResult.PAUSED
    state = State.load(change.state_dir)
    assert state.step == "s2"           # navigation applied first
    assert state.resume_pending is None  # next step starts fresh


@pytest.mark.anyio
async def test_interrupt_does_not_override_resume_goto_target(tmp_path: Path) -> None:
    """A review→agent resume-goto sets resume_pending=target. An interrupt on
    that navigation must KEEP resume_pending=target (not force it to None), so
    the target resumes its transcript."""
    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "coder.md").write_text(
        "---\nname: coder\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n")
    (wf / "agents" / "rev.md").write_text(
        "---\nname: rev\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "  verdict: {type: enum, values: [approve, rework]}\n---\ngo\n")
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\ndefaults: {max_retries: 3, max_loops: 6}\n"
        "phases:\n  - name: only\n    steps:\n"
        "      - {name: code, file: agents/coder.md, tier: low}\n"
        "      - name: review\n        file: agents/rev.md\n        tier: low\n"
        "        rules:\n"
        "          - {if: {verdict: rework}, resume: code}\n"
        "          - {else: true, continue: true}\n")
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (tmp_path / ".imp").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".imp" / "config.yaml").write_text(
        "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n")
    change = _make_change(tmp_path)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    from imp.cli.signals import InterruptCoordinator
    coord = InterruptCoordinator()

    call_count = [0]

    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            call_count[0] += 1
            # First call: code step → Success
            # Second call: review step → rework (triggers resume: code)
            if call_count[0] == 1:
                yield {"type": "agent_output", "payload": {"outcome": "ok", "status": "Success"}}
            else:
                # verdict=rework matches the {if: {verdict: rework}} rule → resume: code
                yield {"type": "agent_output",
                       "payload": {"outcome": "ok", "status": "Success", "verdict": "rework"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()

    resolve.register_provider("fake", env_check=lambda: True, factory=lambda _m: _P())

    def _on_event(e: dict) -> None:
        # Interrupt the instant the review step completes — it routes to
        # resume: code (Goto(code, resume=True)); should_pause is set before
        # navigation runs.
        if (e.get("type") == "step_completed"
                and (e.get("payload") or {}).get("step") == "review"):
            coord._should_pause = True
            coord.pause_reason = "ctrl-c"

    result = await run_change(change, on_event=_on_event, coordinator=coord)
    assert result is RunResult.PAUSED
    state = State.load(change.state_dir)
    assert state.step == "code"
    assert state.resume_pending == "code"  # resume-goto target preserved


@pytest.mark.anyio
async def test_genuine_fatal_no_interrupt_keeps_transcript_for_replay(tmp_path: Path) -> None:
    change = _seed_single_agent(tmp_path)
    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    class _FatalRunner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            yield {"type": "error", "payload": {
                "category": "terminated", "kind": "TerminatedError",
                "message": "agent terminated with an error (cause unknown)"}}
        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _inv: AgentInvocation) -> AgentRunner:
            return _FatalRunner()

    resolve.register_provider("fake", env_check=lambda: True, factory=lambda _m: _P())
    result = await run_change(change)  # no coordinator interrupt
    assert result is RunResult.PAUSED
    state = State.load(change.state_dir)
    # P1: a fatal agent-step pause keeps the current step's transcript so resume
    # replays and continues instead of re-running fresh and re-burning work/quota.
    assert state.resume_pending == state.step


@pytest.mark.anyio
async def test_completion_pending_finalizes_without_redispatch(tmp_path: Path) -> None:
    change = _seed_single_agent(tmp_path)  # single agent step
    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    from imp.cli.signals import InterruptCoordinator

    runs = {"n": 0}

    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            runs["n"] += 1
            yield {"type": "agent_output", "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()

    resolve.register_provider("fake", env_check=lambda: True, factory=lambda _m: _P())

    # Round 1: the only step (named "w") completes cleanly; the on_event hook
    # flips should_pause AT its completion, so navigation runs off the end under
    # an in-flight interrupt → _complete_or_park parks at completion.
    coord = InterruptCoordinator()

    def _on_event(e: dict) -> None:
        if (e.get("type") == "step_completed"
                and (e.get("payload") or {}).get("step") == "w"):
            coord._should_pause = True
            coord.pause_reason = "ctrl-c"

    r1 = await run_change(change, on_event=_on_event, coordinator=coord)
    assert r1 is RunResult.PAUSED
    s1 = State.load(change.state_dir)
    assert s1.completion_pending is True
    assert s1.step is not None  # NOT None (would auto-complete)
    runs_after_round1 = runs["n"]

    # Round 2 (re-entry, fresh coordinator = /continue): finalize via _complete
    # with NO new agent turn.
    resolve.reset_providers_for_tests()
    resolve.register_provider("fake", env_check=lambda: True, factory=lambda _m: _P())
    r2 = await run_change(change)
    assert r2 is RunResult.COMPLETED
    assert runs["n"] == runs_after_round1  # no second agent turn ran
    assert State.load(change.state_dir).status == "completed"


@pytest.mark.anyio
async def test_mid_turn_interrupt_sets_interrupt_pending_true(tmp_path: Path) -> None:
    change = _seed_single_agent(tmp_path)
    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    from imp.cli.signals import InterruptCoordinator
    coord = InterruptCoordinator()

    class _InterruptingRunner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            await coord.signal_pause(reason="ctrl-c")
            yield {"type": "agent_output",
                   "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _inv: AgentInvocation) -> AgentRunner:
            return _InterruptingRunner()

    resolve.register_provider("fake", env_check=lambda: True, factory=lambda _m: _P())

    result = await run_change(change, coordinator=coord)
    assert result is RunResult.PAUSED
    state = State.load(change.state_dir)
    assert state.resume_pending == state.step
    assert state.interrupt_pending is True  # marker shadows the interrupt park


@pytest.mark.anyio
async def test_non_interrupt_pause_overwrites_stale_interrupt_pending(
    tmp_path: Path,
) -> None:
    """A non-interrupt park (here: a fatal agent error, should_pause == False)
    runs the same Pause handler and must assign interrupt_pending = False,
    overwriting a stale True from a prior interrupt that triggered a nudge."""
    change = _seed_single_agent(tmp_path)
    # Seed a stale True (as if a prior interrupt+nudge had set it).
    s = State.load(change.state_dir)
    s.interrupt_pending = True
    s.save(change.state_dir)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()

    class _FatalRunner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            yield {"type": "error", "payload": {
                "category": "terminated", "kind": "TerminatedError",
                "message": "agent terminated with an error (cause unknown)"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _inv: AgentInvocation) -> AgentRunner:
            return _FatalRunner()

    resolve.register_provider("fake", env_check=lambda: True, factory=lambda _m: _P())
    result = await run_change(change)  # no coordinator interrupt
    assert result is RunResult.PAUSED
    state = State.load(change.state_dir)
    assert state.resume_pending == state.step  # fatal park keeps the transcript
    assert state.interrupt_pending is False    # unconditional assignment cleared it


@pytest.mark.anyio
async def test_interrupt_after_complete_clears_interrupt_pending(tmp_path: Path) -> None:
    """Two-step workflow; interrupt fires as step 1 completes (Continue). The
    Continue clear sets interrupt_pending = False before the post-navigation
    pause."""
    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "a.md").write_text(
        "---\nname: a\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n")
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\ndefaults: {max_retries: 3, max_loops: 6}\n"
        "phases:\n  - name: only\n    steps:\n"
        "      - {name: s1, file: agents/a.md, tier: low}\n"
        "      - {name: s2, file: agents/a.md, tier: low}\n")
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (tmp_path / ".imp").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".imp" / "config.yaml").write_text(
        "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n")
    change = _make_change(tmp_path)

    # Seed a stale True; the forward Continue must overwrite it to False.
    s = State.load(change.state_dir)
    s.interrupt_pending = True
    s.save(change.state_dir)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    from imp.cli.signals import InterruptCoordinator
    coord = InterruptCoordinator()

    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            yield {"type": "agent_output",
                   "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()

    resolve.register_provider("fake", env_check=lambda: True, factory=lambda _m: _P())

    def _on_event(e: dict) -> None:
        if (e.get("type") == "step_completed"
                and (e.get("payload") or {}).get("step") == "s1"):
            coord._should_pause = True
            coord.pause_reason = "ctrl-c"

    result = await run_change(change, on_event=_on_event, coordinator=coord)
    assert result is RunResult.PAUSED
    state = State.load(change.state_dir)
    assert state.step == "s2"
    assert state.resume_pending is None
    assert state.interrupt_pending is False  # Continue cleared it


@pytest.mark.anyio
async def test_interrupt_on_resume_goto_clears_interrupt_pending(tmp_path: Path) -> None:
    """The resume-Goto branch repoints resume_pending at its target; it must
    clear interrupt_pending so a stale True does not carry forward onto the
    goto-resume target. Seed a stale True (a single self-resume-goto step, so no
    preceding Continue masks it), drive one rework self-resume-goto, and assert
    the post-navigation interrupt park persists interrupt_pending == False."""
    wf = tmp_path / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    (wf / "agents" / "rev.md").write_text(
        "---\nname: rev\ntools: [complete_step]\n"
        "output:\n  status: {type: enum, values: [Success, Error]}\n"
        "  verdict: {type: enum, values: [approve, rework]}\n---\ngo\n")
    (wf / "workflow.yaml").write_text(
        "name: t\ndescription: t\ndefaults: {max_retries: 3, max_loops: 6}\n"
        "phases:\n  - name: only\n    steps:\n"
        "      - name: review\n        file: agents/rev.md\n        tier: low\n"
        "        rules:\n"
        "          - {if: {verdict: rework}, resume: review}\n"
        "          - {else: true, continue: true}\n")
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    (tmp_path / ".imp").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".imp" / "config.yaml").write_text(
        "providers: [fake]\ntiers:\n  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n")
    change = _make_change(tmp_path)

    # Seed a stale True; the resume-Goto branch must overwrite it to False.
    s = State.load(change.state_dir)
    s.interrupt_pending = True
    s.save(change.state_dir)

    from imp.providers import resolve
    resolve.reset_providers_for_tests()
    from imp.cli.signals import InterruptCoordinator
    coord = InterruptCoordinator()

    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            yield {"type": "agent_output",
                   "payload": {"outcome": "ok", "status": "Success",
                               "verdict": "rework"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"
        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()

    resolve.register_provider("fake", env_check=lambda: True, factory=lambda _m: _P())

    def _on_event(e: dict) -> None:
        if (e.get("type") == "step_completed"
                and (e.get("payload") or {}).get("step") == "review"):
            coord._should_pause = True
            coord.pause_reason = "ctrl-c"

    result = await run_change(change, on_event=_on_event, coordinator=coord)
    assert result is RunResult.PAUSED
    state = State.load(change.state_dir)
    assert state.step == "review"
    assert state.resume_pending == "review"   # resume-goto target preserved
    assert state.interrupt_pending is False   # cleared at the resume-Goto branch
