from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import (
    AgentInvocation,
    AgentRunner,
    Event,
    Message,
    TextBlock,
)
from imp.agents.transcript import Transcript
from imp.engine import executor as executor_mod
from imp.engine.executor import RunResult, run_quest
from imp.engine.quest import Quest
from imp.engine.reincarnation import handoff_path_for, write_transcript_owner
from imp.engine.state import State
from imp.providers import BUILTIN_FLOOR
from tests.test_executor import _persist_initial, _seed_minimal_workflow
from tests.test_executor_provider_pin import _floor_config, _reopen
from tests.test_provider_reconcile_points import _TWO_STEPS, _pre_resolved


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_PICKS_UP = "provider switched — codex picks up this step from the event log"
_NEXT_STEP = "provider switched — codex takes the next step"


def _factory(seen: list[str], quest: Quest, *, arm_on: int,
             script: Callable[[int, AgentInvocation], list[Event]]):
    """A provider whose runner records the model it resolved to, arms the pin
    from inside invocation `arm_on` (what the daemon's set_provider callback
    does to the live Quest), and then yields whatever `script` returns."""

    class _Runner:
        # Public attribute only — see tests/test_no_private_reachthroughs.py.
        def __init__(self, model: str) -> None:
            self.model = model

        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            n = len(seen) + 1
            seen.append(self.model)
            if n == arm_on:
                quest.set_provider("codex")
            for event in script(n, inv):
                yield event

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "x"

        def __init__(self, model: str) -> None:
            self.model = model

        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner(self.model)

    return lambda model: _P(model)


@pytest.mark.anyio
async def test_a_completed_step_confirms_the_switch_takes_the_next_step(
    tmp_path: Path, floor_availability,
) -> None:
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    (tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml").write_text(_TWO_STEPS)
    seen: list[str] = []
    stashed: list[list[str]] = []

    def _script(_n: int, _inv: AgentInvocation) -> list[Event]:
        stashed.append(sorted(
            p.name for p in (quest.quest_dir / "steps" / "only").glob("*.attempt-*")))
        return [{"type": "agent_result",
                 "payload": {"outcome": "ok", "status": "Success"}}]

    floor_availability({"claude_code", "codex"},
                       factory=_factory(seen, quest, arm_on=1, script=_script))
    _pre_resolved(quest, "claude_code")
    events: list[dict[str, Any]] = []

    assert await run_quest(quest, on_event=events.append) is RunResult.COMPLETED

    assert seen == [BUILTIN_FLOOR["claude_code"].tier_defaults["low"],
                    BUILTIN_FLOOR["codex"].tier_defaults["low"]]
    assert {"type": "script_output", "payload": {"stdout": _NEXT_STEP}} in events
    assert not [e for e in events if e["type"] == "reincarnating"]
    assert stashed == [[], []]
    assert {pm.provider for pm in _reopen(quest).resolved_tiers.values()} == {"codex"}


def _error(category: str, kind: str) -> list[Event]:
    return [{"type": "error", "payload": {
        "category": category, "kind": kind, "message": "x"}}]


def _handoff(_n: int, inv: AgentInvocation) -> list[Event]:
    _persist_initial(inv)
    hp = handoff_path_for(inv.transcript_dir)
    hp.write_text("# handoff\n")
    return [{"type": "handoff", "payload": {"handoff_path": str(hp)}}]


_DISPOSITIONS = {
    "fatal": (1, lambda _n, _i: _error("api", "api_error"), None),
    "result_paused": (1, lambda _n, _i: _error("api", "interrupted"), None),
    "on_max_retries": (2, lambda _n, _i: _error("agent_output_missing", "missing"),
                       None),
    "retry_ceiling": (1, lambda _n, _i: _error("rate_limit", "RateLimitError"),
                      "budget"),
    "reincarnation_cap": (1, _handoff, "cap"),
}


@pytest.mark.anyio
@pytest.mark.parametrize("name", sorted(_DISPOSITIONS))
async def test_every_returning_disposition_reconciles_before_it_returns(
    tmp_path: Path, floor_availability, monkeypatch, name: str,
) -> None:
    arm_on, script, extra = _DISPOSITIONS[name]
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    if extra == "budget":
        monkeypatch.setattr(executor_mod, "budget_for", lambda _c: 0.5)
    seen: list[str] = []
    floor_availability({"claude_code", "codex"},
                       factory=_factory(seen, quest, arm_on=arm_on, script=script))
    _pre_resolved(quest, "claude_code")
    if extra == "cap":
        # run_quest only reads context.max_reincarnations on a FRESH resolve
        # (executor.py:537), and these quests are already resolved.
        state = State.load(quest.state_dir)
        state.max_reincarnations = 0
        state.save(quest.state_dir)
    events: list[dict[str, Any]] = []

    assert await run_quest(quest, on_event=events.append) is RunResult.PAUSED

    assert {pm.provider
            for pm in State.load(quest.state_dir).resolved_tiers.values()} == {"codex"}
    assert {"type": "script_output",
            "payload": {"stdout": _PICKS_UP}} in events


@pytest.mark.anyio
async def test_a_pause_after_the_backoff_sleep_applies_the_switch_on_its_way_out(
    tmp_path: Path, floor_availability, monkeypatch,
) -> None:
    async def _pausing(_delay: float, coord: Any, **_kw: Any) -> None:
        await coord.signal_pause()

    monkeypatch.setattr(executor_mod, "_interruptible_sleep", _pausing)
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    seen: list[str] = []
    floor_availability(
        {"claude_code", "codex"},
        factory=_factory(seen, quest, arm_on=1,
                         script=lambda _n, _i: _error("rate_limit", "RateLimitError")))
    _pre_resolved(quest, "claude_code")
    events: list[dict[str, Any]] = []

    assert await run_quest(quest, on_event=events.append) is RunResult.PAUSED

    assert seen == [BUILTIN_FLOOR["claude_code"].tier_defaults["low"]]
    assert {pm.provider
            for pm in State.load(quest.state_dir).resolved_tiers.values()} == {"codex"}
    assert {"type": "script_output", "payload": {"stdout": _PICKS_UP}} in events
    assert [e["payload"]["reason"]
            for e in events if e["type"] == "backoff_ended"] == ["elapsed"]


_SWITCH_SENTENCE = "was switched at the user's request"


def _first_message(inv: AgentInvocation) -> str:
    return (inv.initial_messages[0].content[0].text
            if inv.initial_messages else "")


@pytest.mark.anyio
async def test_a_tier_a_reincarnation_re_enters_on_the_new_vendor(
    tmp_path: Path, floor_availability,
) -> None:
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    seen: list[str] = []
    firsts: list[str] = []

    def _script(n: int, inv: AgentInvocation) -> list[Event]:
        firsts.append(_first_message(inv))
        if n == 1:
            _persist_initial(inv)
            hp = handoff_path_for(inv.transcript_dir)
            hp.write_text("# handoff\nnote body kept\n")
            return [{"type": "handoff", "payload": {"handoff_path": str(hp)}}]
        return [{"type": "agent_result",
                 "payload": {"outcome": "ok", "status": "Success"}}]

    floor_availability({"claude_code", "codex"},
                       factory=_factory(seen, quest, arm_on=1, script=_script))
    _pre_resolved(quest, "claude_code")
    events: list[dict[str, Any]] = []

    assert await run_quest(quest, on_event=events.append) is RunResult.COMPLETED

    assert seen == [BUILTIN_FLOOR["claude_code"].tier_defaults["low"],
                    BUILTIN_FLOOR["codex"].tier_defaults["low"]]
    assert [e["payload"]["reason"]
            for e in events if e["type"] == "reincarnating"] == [
        "context threshold crossed"]
    assert "note body kept" in firsts[1]
    assert _SWITCH_SENTENCE not in firsts[1]
    assert {"type": "script_output", "payload": {"stdout": _PICKS_UP}} in events


@pytest.mark.anyio
async def test_a_tier_b_reincarnation_re_enters_on_the_new_vendor(
    tmp_path: Path, floor_availability,
) -> None:
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    seen: list[str] = []

    def _script(n: int, inv: AgentInvocation) -> list[Event]:
        if n == 1:
            _persist_initial(inv)
            return _error("context_window", "prompt_too_long")
        return [{"type": "agent_result",
                 "payload": {"outcome": "ok", "status": "Success"}}]

    floor_availability({"claude_code", "codex"},
                       factory=_factory(seen, quest, arm_on=1, script=_script))
    _pre_resolved(quest, "claude_code")
    events: list[dict[str, Any]] = []

    assert await run_quest(quest, on_event=events.append) is RunResult.COMPLETED

    assert seen == [BUILTIN_FLOOR["claude_code"].tier_defaults["low"],
                    BUILTIN_FLOOR["codex"].tier_defaults["low"]]
    assert [e["payload"]["reason"]
            for e in events if e["type"] == "reincarnating"] == [
        "context window exceeded"]
    assert {"type": "script_output", "payload": {"stdout": _PICKS_UP}} in events


@pytest.mark.anyio
async def test_the_guards_seed_beats_a_seed_inherited_by_the_errored_retry(
    tmp_path: Path, floor_availability,
) -> None:
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    seen: list[str] = []
    firsts: list[str] = []

    def _script(n: int, inv: AgentInvocation) -> list[Event]:
        firsts.append(_first_message(inv))
        _persist_initial(inv)
        if n == 1:
            hp = handoff_path_for(inv.transcript_dir)
            hp.write_text("# handoff\nearlier generation note\n")
            return [{"type": "handoff", "payload": {"handoff_path": str(hp)}}]
        if n == 2:
            return _error("agent_output_missing", "missing")
        return [{"type": "agent_result",
                 "payload": {"outcome": "ok", "status": "Success"}}]

    floor_availability({"claude_code", "codex"},
                       factory=_factory(seen, quest, arm_on=2, script=_script))
    _pre_resolved(quest, "claude_code")
    events: list[dict[str, Any]] = []

    assert await run_quest(quest, on_event=events.append) is RunResult.COMPLETED

    assert seen[2] == BUILTIN_FLOOR["codex"].tier_defaults["low"]
    assert "earlier generation note" in firsts[1]
    assert _SWITCH_SENTENCE in firsts[2]
    assert "earlier generation note" not in firsts[2]


def _snap(quest: Quest, inv: AgentInvocation) -> dict[str, Any]:
    """Read AT DISPATCH: a completed run archives the quest and reaps its home,
    so nothing under steps/ survives run_quest's return."""
    transcript = inv.transcript_dir / "transcript.jsonl"
    stashes = sorted((quest.quest_dir / "steps" / "only").glob("w.attempt-*"))
    return {
        "stashes": [p.name for p in stashes],
        "stashed": "".join((p / "transcript.jsonl").read_text() for p in stashes),
        "first": _first_message(inv),
        "live": transcript.read_text() if transcript.exists() else "",
    }


def _capturing_sleep(polled: list[bool], *, act=None):
    async def _sleep(_delay: float, coord: Any, *, tick: float = 0.1,
                     wake_when: Any = None) -> None:
        polled.append(wake_when is not None and wake_when())
        if act is not None:
            await act(coord)

    return _sleep


@pytest.mark.anyio
async def test_a_backoff_sleep_wakes_on_the_switch_and_re_enters_fresh(
    tmp_path: Path, floor_availability, monkeypatch,
) -> None:
    polled: list[bool] = []
    monkeypatch.setattr(executor_mod, "_interruptible_sleep",
                        _capturing_sleep(polled))
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    seen: list[str] = []
    snaps: list[dict[str, Any]] = []

    def _script(n: int, inv: AgentInvocation) -> list[Event]:
        snaps.append(_snap(quest, inv))
        if n == 1:
            _persist_initial(inv)
            return _error("rate_limit", "RateLimitError")
        return [{"type": "agent_result",
                 "payload": {"outcome": "ok", "status": "Success"}}]

    floor_availability({"claude_code", "codex"},
                       factory=_factory(seen, quest, arm_on=1, script=_script))
    _pre_resolved(quest, "claude_code")
    events: list[dict[str, Any]] = []

    assert await run_quest(quest, on_event=events.append) is RunResult.COMPLETED

    assert polled == [True]
    assert seen == [BUILTIN_FLOOR["claude_code"].tier_defaults["low"],
                    BUILTIN_FLOOR["codex"].tier_defaults["low"]]
    assert snaps[1]["stashes"] == ["w.attempt-1"]
    assert snaps[1]["live"] == ""
    assert _SWITCH_SENTENCE in snaps[1]["first"]
    assert [e["payload"]["reason"]
            for e in events if e["type"] == "backoff_ended"] == ["provider_switch"]
    assert len([e for e in events if e["type"] == "agent_retry"]) == 1


@pytest.mark.anyio
async def test_a_sleep_with_nothing_pending_falls_through_exactly_as_today(
    tmp_path: Path, floor_availability, monkeypatch,
) -> None:
    polled: list[bool] = []
    monkeypatch.setattr(executor_mod, "_interruptible_sleep",
                        _capturing_sleep(polled))
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    seen: list[str] = []
    snaps: list[dict[str, Any]] = []

    def _script(n: int, inv: AgentInvocation) -> list[Event]:
        snaps.append(_snap(quest, inv))
        if n == 1:
            _persist_initial(inv)
            return _error("rate_limit", "RateLimitError")
        return [{"type": "agent_result",
                 "payload": {"outcome": "ok", "status": "Success"}}]

    floor_availability({"claude_code", "codex"},
                       factory=_factory(seen, quest, arm_on=0, script=_script))
    _pre_resolved(quest, "claude_code")
    events: list[dict[str, Any]] = []

    assert await run_quest(quest, on_event=events.append) is RunResult.COMPLETED

    assert polled == [False]
    assert snaps[1]["stashes"] == []
    assert [e["payload"]["reason"]
            for e in events if e["type"] == "backoff_ended"] == ["elapsed"]
    assert not [e for e in events if e["type"] == "reincarnating"]


@pytest.mark.anyio
async def test_a_retry_now_press_wins_the_reason_over_a_coincident_switch(
    tmp_path: Path, floor_availability, monkeypatch,
) -> None:
    async def _press(coord: Any) -> None:
        coord.request_retry_now()

    polled: list[bool] = []
    monkeypatch.setattr(executor_mod, "_interruptible_sleep",
                        _capturing_sleep(polled, act=_press))
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    seen: list[str] = []

    def _script(n: int, inv: AgentInvocation) -> list[Event]:
        if n == 1:
            _persist_initial(inv)
            return _error("rate_limit", "RateLimitError")
        return [{"type": "agent_result",
                 "payload": {"outcome": "ok", "status": "Success"}}]

    floor_availability({"claude_code", "codex"},
                       factory=_factory(seen, quest, arm_on=1, script=_script))
    _pre_resolved(quest, "claude_code")
    events: list[dict[str, Any]] = []

    assert await run_quest(quest, on_event=events.append) is RunResult.COMPLETED

    assert [e["payload"]["reason"]
            for e in events if e["type"] == "backoff_ended"] == ["retry_now"]
    assert seen[1] == BUILTIN_FLOOR["codex"].tier_defaults["low"]


@pytest.mark.anyio
async def test_an_applied_switch_leaves_a_later_sleep_full_length(
    tmp_path: Path, floor_availability, monkeypatch,
) -> None:
    polled: list[bool] = []
    monkeypatch.setattr(executor_mod, "_interruptible_sleep",
                        _capturing_sleep(polled))
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    (tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml").write_text(_TWO_STEPS)
    seen: list[str] = []

    def _script(n: int, inv: AgentInvocation) -> list[Event]:
        if n == 2:
            _persist_initial(inv)
            return _error("rate_limit", "RateLimitError")
        return [{"type": "agent_result",
                 "payload": {"outcome": "ok", "status": "Success"}}]

    floor_availability({"claude_code", "codex"},
                       factory=_factory(seen, quest, arm_on=1, script=_script))
    _pre_resolved(quest, "claude_code")
    events: list[dict[str, Any]] = []

    assert await run_quest(quest, on_event=events.append) is RunResult.COMPLETED

    assert polled == [False]
    assert [e["payload"]["reason"]
            for e in events if e["type"] == "backoff_ended"] == ["elapsed"]


@pytest.mark.anyio
async def test_a_resumed_entry_reaches_the_same_end_state_through_limb_one(
    tmp_path: Path, floor_availability, monkeypatch,
) -> None:
    polled: list[bool] = []
    monkeypatch.setattr(executor_mod, "_interruptible_sleep",
                        _capturing_sleep(polled))
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    _pre_resolved(quest, "claude_code")
    tdir = quest.quest_dir / "steps" / "only" / "w"
    tdir.mkdir(parents=True, exist_ok=True)
    Transcript(tdir / "transcript.jsonl").append(
        Message(role="assistant", content=(TextBlock(text="still working"),)))
    write_transcript_owner(tdir, "claude_code")
    quest.event_log().append(
        agent_id="step:only/w", type="engine_message",
        payload={"role": "system", "subtype": "system_prompt",
                 "content": [{"type": "text", "text": "stored"}]})
    state = State.load(quest.state_dir)
    state.resume_pending = state.step
    state.save(quest.state_dir)
    seen: list[str] = []
    snaps: list[dict[str, Any]] = []

    def _script(n: int, inv: AgentInvocation) -> list[Event]:
        snaps.append(_snap(quest, inv))
        if n == 1:
            return _error("rate_limit", "RateLimitError")
        return [{"type": "agent_result",
                 "payload": {"outcome": "ok", "status": "Success"}}]

    floor_availability({"claude_code", "codex"},
                       factory=_factory(seen, quest, arm_on=1, script=_script))
    events: list[dict[str, Any]] = []

    assert await run_quest(quest, on_event=events.append) is RunResult.COMPLETED

    assert polled == [True]
    assert snaps[0]["live"].strip() != ""
    assert seen == [BUILTIN_FLOOR["claude_code"].tier_defaults["low"],
                    BUILTIN_FLOOR["codex"].tier_defaults["low"]]
    assert snaps[1]["stashes"] == ["w.attempt-1"]
    assert snaps[1]["live"] == ""
    assert "still working" in snaps[1]["stashed"]
    assert _SWITCH_SENTENCE in snaps[1]["first"]


@pytest.mark.anyio
async def test_a_switch_applied_at_the_step_boundary_also_stops_being_pending(
    tmp_path: Path, floor_availability, monkeypatch,
) -> None:
    polled: list[bool] = []
    monkeypatch.setattr(executor_mod, "_interruptible_sleep",
                        _capturing_sleep(polled))
    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    (tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml").write_text(_TWO_STEPS)
    seen: list[str] = []

    real_dispatch = executor_mod._dispatch_step

    async def _arm_between_steps(*a: Any, **k: Any) -> Any:
        out = await real_dispatch(*a, **k)
        if len(seen) == 1:
            quest.set_provider("codex")
        return out

    monkeypatch.setattr(executor_mod, "_dispatch_step", _arm_between_steps)

    def _script(n: int, inv: AgentInvocation) -> list[Event]:
        if n == 2:
            _persist_initial(inv)
            return _error("rate_limit", "RateLimitError")
        return [{"type": "agent_result",
                 "payload": {"outcome": "ok", "status": "Success"}}]

    floor_availability({"claude_code", "codex"},
                       factory=_factory(seen, quest, arm_on=0, script=_script))
    _pre_resolved(quest, "claude_code")
    events: list[dict[str, Any]] = []

    assert await run_quest(quest, on_event=events.append) is RunResult.COMPLETED

    assert seen[1] == BUILTIN_FLOOR["codex"].tier_defaults["low"]
    assert polled == [False]
    assert [e["payload"]["reason"]
            for e in events if e["type"] == "backoff_ended"] == ["elapsed"]


def test_every_route_agent_return_reconciles_before_it_leaves() -> None:
    src = textwrap.dedent(inspect.getsource(executor_mod._route_agent))
    fn = ast.parse(src).body[0]
    returns = sorted(n.lineno for n in ast.walk(fn) if isinstance(n, ast.Return))
    assert len(returns) == 13, (
        f"_route_agent has {len(returns)} return paths, not 13 — a path was "
        "added or removed; every one must reconcile before it returns or re-enters")
    lines = src.splitlines()
    for lineno in returns:
        prior = lines[:lineno - 1]
        last_reconcile = max((i for i, ln in enumerate(prior)
                              if "_reconcile_provider(" in ln), default=-1)
        last_return = max((i for i, ln in enumerate(prior)
                           if ln.lstrip().startswith("return ")), default=-1)
        assert last_reconcile > last_return, (
            f"the return at line {lineno} of _route_agent does not reconcile first")
