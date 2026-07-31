"""Part 2: the transient-error retry arms resume_should_continue=True, feeds it into
resume_should_continue at the consume site, clears the pair on forward progress,
and sets its disposition from the pause reason."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

from imp.engine.executor import RunResult, run_quest
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.paths import project_state_dir


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_WRITER = (
    "---\nname: writer\ntools: [complete_step]\n"
    "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
)
_SINGLE_WF = (
    "name: t\ndescription: t\n"
    "defaults: {max_retries: 1, max_loops: 6, on_max_retries: {pause: true}}\n"
    "phases:\n"
    "  - name: only\n"
    "    steps:\n"
    "      - {name: w, file: agents/writer.md, tier: low}\n"
)


def _make_quest(root: Path, *, agents: dict[str, str], workflow_yaml: str) -> Quest:
    from imp.engine.loader import load_workflow
    from imp.providers.effective import load_effective_config

    wf = root / "imp" / "workflows" / "imp"
    (wf / "agents").mkdir(parents=True)
    for fname, body in agents.items():
        (wf / "agents" / fname).write_text(body)
    (wf / "workflow.yaml").write_text(workflow_yaml)

    (root / "imp").mkdir(parents=True, exist_ok=True)
    (root / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    (project_state_dir(root) / "config.yaml").write_text(
        "providers: [fake]\ntiers:\n"
        "  low: {fake: m}\n  medium: {fake: m}\n  high: {fake: m}\n"
    )

    quest_dir = project_state_dir(root) / "quests" / "c"
    quest_dir.mkdir(parents=True, exist_ok=True)
    (quest_dir / "state").mkdir(exist_ok=True)
    (quest_dir / "quest.json").write_text(json.dumps(
        {"id": "c", "title": "t", "base_branch": "main"}, indent=2, sort_keys=True))

    configured_tiers: list[str] = []
    with contextlib.suppress(Exception):
        configured_tiers = load_effective_config(control_root=root).config.tier_names()

    workflow = load_workflow(wf, configured_tiers=configured_tiers)
    flat = workflow.flat_steps()
    State(
        id="c", step=flat[0].name if flat else None, step_attempt=0,
        status="running", vars=dict(workflow.vars.defaults()), loop_counts={},
    ).save(quest_dir / "state")

    quest = Quest(
        id="c", title="t", base_branch="main",
        control_root=root, project_root=root, quest_dir=quest_dir,
    )
    quest.event_log().append(
        agent_id=None, type="quest_created", payload={"id": "c", "title": "t"})
    return quest


class _StallNthRunner:
    """Emits a 'stalled' error (→ RetryWithBackoff) when `stalled`, else succeeds."""

    def __init__(self, stalled: bool) -> None:
        self._stalled = stalled

    async def run(self, invocation: Any) -> Any:
        if self._stalled:
            yield {"type": "error", "payload": {
                "category": "stalled", "kind": "stalled",
                "message": "no events for 240s"}}
        else:
            yield {"type": "agent_result",
                   "payload": {"status": "Success", "outcome": "done"}}

    async def interrupt(self) -> None:
        pass


class _StallThenSucceedProvider:
    name = "fake"
    model = "m"

    def __init__(self, stalls: int) -> None:
        self.stalls = stalls
        self.drives = 0

    def create_runner(self, _inv: Any) -> _StallNthRunner:
        self.drives += 1
        return _StallNthRunner(self.drives <= self.stalls)


@pytest.mark.anyio
async def test_backoff_persists_resume_continue_before_sleep(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(
        tmp_path, agents={"writer.md": _WRITER}, workflow_yaml=_SINGLE_WF)
    provider = _StallThenSucceedProvider(stalls=1)
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)

    seen_on_disk: list[tuple[str | None, bool]] = []

    async def _capture_sleep(_delay: float, _coord: Any) -> None:
        st = State.load(quest.state_dir)
        seen_on_disk.append((st.resume_pending, st.resume_should_continue))

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _capture_sleep)

    result = await run_quest(quest)
    assert result is RunResult.COMPLETED
    assert provider.drives == 2               # one stall + one success
    # The pair was armed AND persisted to disk BEFORE the backoff sleep ran.
    assert seen_on_disk == [("w", True)]


class _RecordingProvider:
    """Records invocation.resume_should_continue at each drive; stalls the first
    `stalls` drives, then succeeds."""

    name = "fake"
    model = "m"

    def __init__(self, stalls: int) -> None:
        self.stalls = stalls
        self.drives = 0
        self.seen: list[bool] = []
        self.causes: list[str] = []

    def create_runner(self, inv: Any) -> _StallNthRunner:
        self.seen.append(inv.resume_should_continue)
        self.causes.append(inv.resume_cause)
        self.drives += 1
        return _StallNthRunner(self.drives <= self.stalls)


@pytest.mark.anyio
async def test_backoff_respawn_receives_resume_should_continue(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(
        tmp_path, agents={"writer.md": _WRITER}, workflow_yaml=_SINGLE_WF)
    provider = _RecordingProvider(stalls=1)
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)

    async def _no_sleep(_delay: float, _coord: Any) -> None:
        return None

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    result = await run_quest(quest)
    assert result is RunResult.COMPLETED
    # First drive: fresh state, not armed → resume_should_continue False.
    # Second drive (respawn after backoff): the live read
    # resume_pending == state.step with resume_should_continue → True.
    assert provider.seen == [False, True]
    # The backoff arming carries the cause through to the respawned invocation, so a
    # provider selects the retry note rather than the human pause copy.
    assert provider.causes == ["human", "provider_retry"]


_A = (
    "---\nname: a\ntools: [complete_step]\n"
    "output:\n  status: {type: enum, values: [Success, Error]}\n---\ngo\n"
)
_TWO_STEP_WF = (
    "name: t\ndescription: t\ndefaults: {max_retries: 3, max_loops: 6}\n"
    "phases:\n  - name: only\n    steps:\n"
    "      - {name: s1, file: agents/a.md, tier: low}\n"
    "      - {name: s2, file: agents/a.md, tier: low}\n"
)
_REV = (
    "---\nname: rev\ntools: [complete_step]\n"
    "output:\n  status: {type: enum, values: [Success, Error]}\n"
    "  verdict: {type: enum, values: [approve, rework]}\n---\ngo\n"
)
_RESUME_GOTO_WF = (
    "name: t\ndescription: t\ndefaults: {max_retries: 3, max_loops: 6}\n"
    "phases:\n  - name: only\n    steps:\n"
    "      - name: review\n        file: agents/rev.md\n        tier: low\n"
    "        rules:\n"
    "          - {if: {verdict: rework}, resume: review}\n"
    "          - {else: true, continue: true}\n"
)


class _SuccessRunner:
    async def run(self, invocation: Any) -> Any:
        yield {"type": "agent_result",
               "payload": {"outcome": "ok", "status": "Success"}}

    async def interrupt(self) -> None:
        pass


class _SuccessProvider:
    name = "fake"
    model = "m"

    def create_runner(self, _inv: Any) -> _SuccessRunner:
        return _SuccessRunner()


class _ReworkRunner:
    async def run(self, invocation: Any) -> Any:
        yield {"type": "agent_result",
               "payload": {"outcome": "ok", "status": "Success",
                           "verdict": "rework"}}

    async def interrupt(self) -> None:
        pass


class _ReworkProvider:
    name = "fake"
    model = "m"

    def create_runner(self, _inv: Any) -> _ReworkRunner:
        return _ReworkRunner()


@pytest.mark.anyio
async def test_completion_clears_resume_pair(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(
        tmp_path, agents={"writer.md": _WRITER}, workflow_yaml=_SINGLE_WF)
    _prov = _StallThenSucceedProvider(stalls=1)
    provider_registry(
        "fake", env_check=lambda: True,
        factory=lambda _m: _prov)

    async def _no_sleep(_delay: float, _coord: Any) -> None:
        return None

    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    result = await run_quest(quest)
    assert result is RunResult.COMPLETED
    from imp.engine.archive import read as archive_read
    archive_read.rehydrate_quest(quest.control_root, quest.id)  # home reaped on done
    # The in-loop arm left the pair set; the terminal Continue must clear it.
    st = State.load(quest.state_dir)
    assert st.resume_pending is None
    assert st.resume_should_continue is False


@pytest.mark.anyio
async def test_forward_advance_clears_resume_pair(
    tmp_path: Path, provider_registry: Any,
) -> None:
    quest = _make_quest(
        tmp_path, agents={"a.md": _A}, workflow_yaml=_TWO_STEP_WF)
    # Seed a stale continue; the forward Continue (s1 → s2) must clear it.
    s = State.load(quest.state_dir)
    s.arm_step_resume(should_continue=True)   # stale continue the forward move must clear
    s.save(quest.state_dir)

    from imp.cli.signals import InterruptCoordinator
    coord = InterruptCoordinator()
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: _SuccessProvider())

    def _on_event(e: dict) -> None:
        if (e.get("type") == "step_completed"
                and (e.get("payload") or {}).get("step") == "s1"):
            coord._should_pause = True
            coord.pause_reason = "ctrl-c"

    result = await run_quest(quest, on_event=_on_event, coordinator=coord)
    assert result is RunResult.PAUSED
    state = State.load(quest.state_dir)
    assert state.step == "s2"
    assert state.resume_pending is None    # Continue cleared the pair
    assert state.resume_should_continue is False


@pytest.mark.anyio
async def test_resume_goto_arms_solicit(
    tmp_path: Path, provider_registry: Any,
) -> None:
    quest = _make_quest(
        tmp_path, agents={"rev.md": _REV}, workflow_yaml=_RESUME_GOTO_WF)
    # Seed a stale continue; the resume-Goto branch must overwrite it.
    s = State.load(quest.state_dir)
    s.arm_step_resume(should_continue=True)   # stale; the resume-Goto must overwrite it
    s.save(quest.state_dir)

    from imp.cli.signals import InterruptCoordinator
    coord = InterruptCoordinator()
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: _ReworkProvider())

    def _on_event(e: dict) -> None:
        if (e.get("type") == "step_completed"
                and (e.get("payload") or {}).get("step") == "review"):
            coord._should_pause = True
            coord.pause_reason = "ctrl-c"

    result = await run_quest(quest, on_event=_on_event, coordinator=coord)
    assert result is RunResult.PAUSED
    state = State.load(quest.state_dir)
    assert state.step == "review"
    assert state.resume_pending == "review"   # resume-goto re-armed the target
    assert state.resume_should_continue is False      # a rework re-entry solicits


class _AlwaysStallProvider:
    name = "fake"
    model = "m"

    def create_runner(self, _inv: Any) -> _StallNthRunner:
        return _StallNthRunner(stalled=True)


class _FatalRunner:
    async def run(self, invocation: Any) -> Any:
        yield {"type": "error", "payload": {
            "category": "terminated", "kind": "TerminatedError",
            "message": "agent terminated with an error (cause unknown)"}}

    async def interrupt(self) -> None:
        pass


class _FatalProvider:
    name = "fake"
    model = "m"

    def create_runner(self, _inv: Any) -> _FatalRunner:
        return _FatalRunner()


@pytest.mark.anyio
async def test_retry_ceiling_pause_preserves_marker(
    tmp_path: Path, provider_registry: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest = _make_quest(
        tmp_path, agents={"writer.md": _WRITER}, workflow_yaml=_SINGLE_WF)
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: _AlwaysStallProvider())

    # Force a FIRST-attempt ceiling (budget 0). The backoff loop arms
    # resume_should_continue=True only AFTER the budget check passes, so with a
    # zero budget that arm never runs and the pair is still unset when the
    # retry_ceiling Pause is returned — isolating the Pause handler's disposition
    # line as the sole cause of the armed continue. Drop that line and this fails.
    monkeypatch.setattr("imp.engine.executor.budget_for", lambda _category: 0.0)

    events: list[dict] = []
    result = await run_quest(quest, on_event=events.append)
    assert result is RunResult.PAUSED
    paused = [e for e in events if e["type"] == "workflow_paused"]
    assert paused and paused[0]["payload"]["reason"] == "retry_ceiling"
    # Not armed at ceiling time; the retry_ceiling disposition armed continue.
    st = State.load(quest.state_dir)
    assert st.resume_pending == st.step
    assert st.resume_should_continue is True   # retry_ceiling give-up continues on resume


@pytest.mark.anyio
async def test_fatal_pause_clears_marker(
    tmp_path: Path, provider_registry: Any,
) -> None:
    quest = _make_quest(
        tmp_path, agents={"writer.md": _WRITER}, workflow_yaml=_SINGLE_WF)
    # Seed a stale continue (as if a prior backoff had armed it).
    s = State.load(quest.state_dir)
    s.arm_step_resume(should_continue=True)   # stale from a prior backoff
    s.save(quest.state_dir)

    provider_registry("fake", env_check=lambda: True, factory=lambda _m: _FatalProvider())

    result = await run_quest(quest)  # no coordinator interrupt
    assert result is RunResult.PAUSED
    state = State.load(quest.state_dir)
    assert state.resume_pending == state.step   # fatal park keeps the transcript
    assert state.resume_should_continue is False        # non-retry park solicits on resume


@pytest.mark.anyio
async def test_ctrl_c_park_arms_continue(
    tmp_path: Path, provider_registry: Any,
) -> None:
    quest = _make_quest(
        tmp_path, agents={"writer.md": _WRITER}, workflow_yaml=_SINGLE_WF)
    # Seed a stale continue; a Ctrl-C park during a transient backoff re-arms it.
    s = State.load(quest.state_dir)
    s.arm_step_resume(should_continue=True)
    s.save(quest.state_dir)

    from imp.cli.signals import InterruptCoordinator
    coord = InterruptCoordinator()

    class _InterruptingRunner:
        async def run(self, invocation: Any) -> Any:
            await coord.signal_pause(reason="ctrl-c")
            yield {"type": "agent_result",
                   "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"

        def create_runner(self, _inv: Any) -> _InterruptingRunner:
            return _InterruptingRunner()

    provider_registry("fake", env_check=lambda: True, factory=lambda _m: _P())

    result = await run_quest(quest, coordinator=coord)
    assert result is RunResult.PAUSED
    state = State.load(quest.state_dir)
    assert state.resume_pending == state.step
    assert state.resume_should_continue is True   # Ctrl-C park continues on resume
