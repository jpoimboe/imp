"""Reason-aware lane eviction: a clean interrupt / transient give-up in the held
integration region KEEPS the merge lane and resumes in place; rework/crash still
evict. Held-region harness = a `lane` acquire step followed by the interruptible
work step(s)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

import imp.remote.session.attach as attach_mod
from imp.agents.runner import AgentInvocation, AgentRunner, Event
from imp.cli.signals import InterruptCoordinator
from imp.engine import merge_queue as mq
from imp.engine.executor import RunResult, run_change
from imp.engine.quest import Quest
from imp.engine.state import JsonlEventLog, State
from tests.conftest import _git, _init_repo


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_AGENT = (
    "---\nname: a\ntools: [complete_step]\n"
    "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
)

# lane (acquire) -> held (agent) -> after (agent)
_AGENT_HELD_WF = (
    "name: t\ndescription: t\n"
    "defaults: {max_retries: 1, max_loops: 6, on_max_retries: {pause: true}}\n"
    "phases:\n"
    "  - name: integrate\n"
    "    steps:\n"
    "      - name: lane\n"
    "        acquire: merge-lane\n"
    "        rules:\n"
    "          - {else: true, continue: true}\n"
    "      - {name: held, file: agents/a.md, tier: low}\n"
    "      - {name: after, file: agents/a.md, tier: low}\n"
)

# lane (acquire) -> verify (interruptible command) -> after (agent)
_CMD_HELD_WF = (
    "name: t\ndescription: t\n"
    "defaults: {max_retries: 1, max_loops: 6, on_max_retries: {pause: true}}\n"
    "phases:\n"
    "  - name: integrate\n"
    "    steps:\n"
    "      - name: lane\n"
    "        acquire: merge-lane\n"
    "        rules:\n"
    "          - {else: true, continue: true}\n"
    "      - name: verify\n"
    "        command: \"sleep 5\"\n"
    "        interruptible: true\n"
    "        rules:\n"
    "          - {if: {exit: 0}, continue: true}\n"
    "          - {else: true, pause: true}\n"
    "      - {name: after, file: agents/a.md, tier: low}\n"
)


def _seed_wf(root: Path, workflow_yaml: str, *, sandbox_disabled: bool = False) -> None:
    """Commit a fake-provider workflow with an agents/a.md file and the given
    workflow.yaml, so Quest.create can fork a worktree from main."""
    _init_repo(root)
    src = root / "imp" / "workflows" / "imp"
    (src / "agents").mkdir(parents=True)
    (src / "agents" / "a.md").write_text(_AGENT)
    (src / "workflow.yaml").write_text(workflow_yaml)
    (root / "imp" / "project.yaml").write_text(
        "default-workflow: imp\ndefault-base: main\n")
    (root / ".imp").mkdir(exist_ok=True)
    cfg = ("providers: [fake]\n"
           "tiers:\n"
           "  low: {fake: m}\n"
           "  medium: {fake: m}\n"
           "  high: {fake: m}\n")
    if sandbox_disabled:
        cfg += "sandbox:\n  enabled: false\n"
    (root / ".imp" / "config.yaml").write_text(cfg)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "imp setup")


def _provider(runner_factory):
    class _P:
        name = "fake"
        model = "m"

        def create_runner(self, _inv: AgentInvocation) -> AgentRunner:
            return runner_factory()
    return _P()


def _types(ch: Quest) -> list[str]:
    return [e.get("type") for e in JsonlEventLog(ch.events_path).read()]


@pytest.mark.anyio
async def test_agent_interrupt_in_held_region_keeps_lane_and_resumes_in_place(
    tmp_path: Path, provider_registry: Any,
) -> None:
    """AC1: a Ctrl-C interrupt at an agent step in the held region keeps the lane,
    stays merge-queue head, sets lane_retained, and resumes on the SAME step."""
    coord = InterruptCoordinator()

    class _InterruptingRunner:
        async def run(self, _inv: AgentInvocation) -> AsyncIterator[Event]:
            # Mid-turn Ctrl-C: flip should_pause, then yield one event so the
            # per-event should_pause check returns a clean cooperative interrupt.
            await coord.signal_pause(reason="ctrl-c")
            yield {"type": "agent_output",
                   "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    provider_registry("fake", env_check=lambda: True,
                      factory=lambda _m: _provider(_InterruptingRunner))
    _seed_wf(tmp_path, _AGENT_HELD_WF)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")

    result = await run_change(ch, coordinator=coord)

    assert result is RunResult.PAUSED
    st = State.load(ch.state_dir)
    assert "lane_evicted" not in _types(ch)          # no eviction
    assert st.held_lane == "main"                    # lane still held
    assert st.held_acquire_step == "lane"            # acquire step unchanged
    assert st.step == "held"                          # cursor on the interrupted step
    assert st.resume_pending == "held"               # armed for in-place resume
    assert st.lane_retained is True
    assert mq.is_head(tmp_path, "feat", "main")      # still queue head


@pytest.mark.anyio
async def test_command_interrupt_in_held_region_keeps_lane(
    tmp_path: Path, provider_registry: Any,
) -> None:
    """AC2: an interrupted interruptible command step reaches the mid-flight Pause
    path holding the lane. Because it is not an agent step, arm_step_resume does
    NOT run — the command re-runs from scratch on resume (cursor unchanged)."""
    coord = InterruptCoordinator()

    class _SuccessRunner:  # only the `after` agent (never reached here)
        async def run(self, _inv: AgentInvocation) -> AsyncIterator[Event]:
            yield {"type": "agent_output",
                   "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    provider_registry("fake", env_check=lambda: True,
                      factory=lambda _m: _provider(_SuccessRunner))
    _seed_wf(tmp_path, _CMD_HELD_WF, sandbox_disabled=True)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")

    def _on_event(e: dict) -> None:
        # Flip should_pause the instant the interruptible `verify` command begins,
        # so run_script's abort predicate fires and raises ScriptInterrupted.
        if (e.get("type") == "step_started"
                and (e.get("payload") or {}).get("step") == "verify"):
            coord._should_pause = True
            coord.pause_reason = "ctrl-c"

    result = await run_change(ch, on_event=_on_event, coordinator=coord)

    assert result is RunResult.PAUSED
    st = State.load(ch.state_dir)
    assert "lane_evicted" not in _types(ch)
    assert st.held_lane == "main"
    assert st.held_acquire_step == "lane"
    assert st.step == "verify"                        # cursor on the same step
    assert st.resume_pending is None                  # NOT armed (not an agent step)
    assert st.lane_retained is True
    assert mq.is_head(tmp_path, "feat", "main")


@pytest.mark.anyio
async def test_transient_giveup_in_held_region_keeps_lane(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5: a retry_ceiling give-up at an agent step in the held region keeps the
    lane (no interrupt involved) and resumes in place."""
    class _StallRunner:
        async def run(self, _inv: AgentInvocation) -> AsyncIterator[Event]:
            yield {"type": "error", "payload": {
                "category": "stalled", "kind": "stalled",
                "message": "no events for 240s"}}

        async def interrupt(self) -> None:
            pass

    provider_registry("fake", env_check=lambda: True,
                      factory=lambda _m: _provider(_StallRunner))
    _seed_wf(tmp_path, _AGENT_HELD_WF)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    # Zero budget => the first backoff attempt exceeds it => retry_ceiling Pause,
    # with no real sleep. (Same lever as test_retry_ceiling_pause_preserves_marker.)
    monkeypatch.setattr("imp.engine.executor.budget_for", lambda _category: 0.0)

    result = await run_change(ch)

    assert result is RunResult.PAUSED
    st = State.load(ch.state_dir)
    assert "lane_evicted" not in _types(ch)
    assert st.held_lane == "main"
    assert st.held_acquire_step == "lane"
    assert st.lane_retained is True
    assert st.resume_pending == "held"                # agent step armed in place
    assert mq.is_head(tmp_path, "feat", "main")


@pytest.mark.anyio
async def test_post_navigation_interrupt_keeps_lane(
    tmp_path: Path, provider_registry: Any,
) -> None:
    """AC3: a held-region agent step completes (Continue) as an interrupt is
    observed. Navigation has already advanced the cursor; the post-navigation
    branch must retain the lane and leave the cursor on the NAVIGATED-TO step."""
    coord = InterruptCoordinator()

    class _CleanRunner:
        async def run(self, _inv: AgentInvocation) -> AsyncIterator[Event]:
            # Complete cleanly (=> Continue). The interrupt is injected AFTER this
            # step completes, via the on_event hook — never inside run().
            yield {"type": "agent_output",
                   "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    provider_registry("fake", env_check=lambda: True,
                      factory=lambda _m: _provider(_CleanRunner))
    _seed_wf(tmp_path, _AGENT_HELD_WF)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")

    def _on_event(e: dict) -> None:
        # Flip should_pause synchronously the instant `held` completes — the
        # interrupt-after-completion window that reaches the post-nav branch.
        if (e.get("type") == "step_completed"
                and (e.get("payload") or {}).get("step") == "held"):
            coord._should_pause = True
            coord.pause_reason = "ctrl-c"

    result = await run_change(ch, on_event=_on_event, coordinator=coord)

    assert result is RunResult.PAUSED
    st = State.load(ch.state_dir)
    assert "lane_evicted" not in _types(ch)
    assert st.held_lane == "main"
    assert st.held_acquire_step == "lane"
    assert st.step == "after"                         # navigated-to step, not rewound
    assert st.lane_retained is True
    assert mq.is_head(tmp_path, "feat", "main")


@pytest.mark.anyio
async def test_retained_lane_resume_does_not_reevict_at_entry(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4: resuming from a retained-lane paused state runs the entry guard
    WITHOUT evicting, keeps the lane, and clears lane_retained — persisted to disk
    BEFORE the resumed step dispatches."""
    # Sole holder never wakes a next head; stub the daemon spawn defensively.
    monkeypatch.setattr(attach_mod, "spawn_session_daemon", lambda *a, **k: None)

    class _SuccessRunner:
        async def run(self, _inv: AgentInvocation) -> AsyncIterator[Event]:
            yield {"type": "agent_output",
                   "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    provider_registry("fake", env_check=lambda: True,
                      factory=lambda _m: _provider(_SuccessRunner))
    _seed_wf(tmp_path, _AGENT_HELD_WF)
    ch = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")

    # Reconstruct the AC1 paused-with-retained state on disk.
    mq.enqueue_if_absent(tmp_path, "feat", "main")
    st = State.load(ch.state_dir)
    st.held_lane = "main"
    st.held_acquire_step = "lane"
    st.step = "held"
    st.resume_pending = "held"
    st.lane_retained = True
    st.status = "paused"
    st.save(ch.state_dir)

    disk_flag_at_dispatch: dict[str, Any] = {}

    def _on_event(e: dict) -> None:
        # At the first step dispatch after the entry guard, the cleared flag must
        # already be on disk (persist-before-dispatch). Capture only the first.
        if (e.get("type") == "step_started"
                and "value" not in disk_flag_at_dispatch):
            disk_flag_at_dispatch["value"] = State.load(ch.state_dir).lane_retained

    await run_change(ch, on_event=_on_event)

    assert "lane_evicted" not in _types(ch)           # entry guard did NOT evict
    assert disk_flag_at_dispatch["value"] is False    # cleared + persisted first
    assert State.load(ch.state_dir).lane_retained is False
