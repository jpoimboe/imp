from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner, Event, Message, TextBlock
from imp.agents.transcript import Transcript
from imp.engine.executor import RunResult, run_quest
from imp.engine.quest import Quest
from imp.engine.reincarnation import handoff_path_for, write_transcript_owner
from imp.engine.state import CurrentStep, State
from imp.paths import project_state_dir
from imp.providers import BUILTIN_FLOOR
from imp.providers.config import ProviderModel
from tests.conftest import _seed_control
from tests.test_executor_resume_rerun import _seed_single_agent


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_SWITCH_SENTENCE = "was switched at the user's request"


def _tdir(quest: Quest) -> Path:
    return quest.quest_dir / "steps" / "only" / "w"


def _read(path: Path) -> str | None:
    return path.read_text() if path.exists() else None


def _snapshot(quest: Quest, inv: AgentInvocation) -> dict[str, Any]:
    """Every on-disk fact this suite asserts, read AT DISPATCH: a completed run
    reaps the whole quest home, so nothing under steps/ survives the return."""
    tdir = _tdir(quest)
    stashes = sorted((quest.quest_dir / "steps" / "only").glob("w.attempt-*"))
    events = [json.loads(line)
              for line in quest.events_path.read_text().splitlines() if line]
    return {
        "first_message": (inv.initial_messages[0].content[0].text
                          if inv.initial_messages else ""),
        "stashes": [s.name for s in stashes],
        "stashed_transcript": "".join(
            (s / "transcript.jsonl").read_text() for s in stashes),
        "stashed_sidecar": [_read(s / "provider") for s in stashes],
        "stashed_note": [_read(s / "w.handoff.md") for s in stashes],
        "live_note": _read(handoff_path_for(tdir)),
        "live_sidecar": _read(tdir / "provider"),
        "live_transcript": _read(tdir / "transcript.jsonl") or "",
        "reincarnating": [e["payload"]["reason"]
                          for e in events if e["type"] == "reincarnating"],
        "reincarnations": State.load(quest.state_dir).reincarnation_count(
            "step:only/w"),
        "state_json": (quest.state_dir / "state.json").read_text(),
    }


def _seed_transcript(quest: Quest, owner: str | None, *, body: str = "x") -> Path:
    tdir = _tdir(quest)
    tdir.mkdir(parents=True, exist_ok=True)
    if body:
        Transcript(tdir / "transcript.jsonl").append(
            Message(role="assistant", content=(TextBlock(text=body),)))
    else:
        (tdir / "transcript.jsonl").write_text("")
    if owner is not None:
        write_transcript_owner(tdir, owner)
    # In production a step with a transcript always has its stored system prompt.
    quest.event_log().append(
        agent_id="step:only/w", type="engine_message",
        payload={"role": "system", "subtype": "system_prompt",
                 "content": [{"type": "text", "text": "stored"}]})
    return tdir


def _arm_resume(quest: Quest) -> None:
    state = State.load(quest.state_dir)
    state.resume_pending = state.step
    state.save(quest.state_dir)


def _mark_in_flight(quest: Quest, tdir: Path) -> None:
    state = State.load(quest.state_dir)
    state.current_step = CurrentStep(transcript_dir=tdir)
    state.save(quest.state_dir)


def _register(provider_registry, quest: Quest, seen: list[dict[str, Any]],
              names: tuple[str, ...] = ("fake",)) -> None:
    class _Runner:
        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            seen.append(_snapshot(quest, inv))
            yield {"type": "agent_result",
                   "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "fake"
        model = "m"

        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner()

    for name in names:
        provider_registry(name, env_check=lambda: True, factory=lambda _m: _P())


@pytest.mark.anyio
async def test_a_foreign_transcript_a_resume_would_replay_is_retired(
    tmp_path: Path, provider_registry,
) -> None:
    # Limb 1 — and no reconcile coincides with this dispatch: a rule-emitted
    # Goto(resume=True) reaches a step whose live transcript predates the switch,
    # proving the guard is keyed on the transcript rather than on the switch.
    quest = _seed_single_agent(tmp_path)
    _seed_transcript(quest, "codex", body="from the other vendor")
    _arm_resume(quest)
    seen: list[dict[str, Any]] = []
    _register(provider_registry, quest, seen)

    assert await run_quest(quest) is RunResult.COMPLETED

    snap = seen[0]
    assert snap["stashes"] == ["w.attempt-1"]
    assert "from the other vendor" in snap["stashed_transcript"]
    assert snap["stashed_sidecar"] == ["codex"]      # travelled with the dir
    assert _SWITCH_SENTENCE in snap["first_message"]
    # AC 12(c): the seed is built and delivered in memory — State gains no slot
    # that could be left set and re-seed a later step.
    assert _SWITCH_SENTENCE not in snap["state_json"]


@pytest.mark.anyio
async def test_a_matching_sidecar_replays_untouched(
    tmp_path: Path, provider_registry,
) -> None:
    quest = _seed_single_agent(tmp_path)
    _seed_transcript(quest, "fake", body="from the same vendor")
    _arm_resume(quest)
    seen: list[dict[str, Any]] = []
    _register(provider_registry, quest, seen)

    assert await run_quest(quest) is RunResult.COMPLETED

    snap = seen[0]
    assert snap["stashes"] == []
    assert "from the same vendor" in snap["live_transcript"]
    assert _SWITCH_SENTENCE not in snap["first_message"]


@pytest.mark.anyio
async def test_an_unlabelled_directory_counts_as_not_matching(
    tmp_path: Path, provider_registry,
) -> None:
    quest = _seed_single_agent(tmp_path)
    _seed_transcript(quest, None)
    _arm_resume(quest)
    seen: list[dict[str, Any]] = []
    _register(provider_registry, quest, seen)

    await run_quest(quest)

    assert seen[0]["stashes"] == ["w.attempt-1"]
    assert _SWITCH_SENTENCE in seen[0]["first_message"]


@pytest.mark.anyio
async def test_a_first_entry_in_flight_attempt_is_stashed_not_wiped(
    tmp_path: Path, provider_registry,
) -> None:
    # Limb 2, the feature's headline path: a step that died on quota during its
    # FIRST entry has is_resume False, so rmtree would silently destroy it.
    quest = _seed_single_agent(tmp_path)
    tdir = _seed_transcript(quest, "codex", body="half-finished work")
    _mark_in_flight(quest, tdir)
    seen: list[dict[str, Any]] = []
    _register(provider_registry, quest, seen)

    await run_quest(quest)

    snap = seen[0]
    assert snap["stashes"] == ["w.attempt-1"]
    assert "half-finished work" in snap["stashed_transcript"]
    assert _SWITCH_SENTENCE in snap["first_message"]


@pytest.mark.anyio
async def test_an_empty_transcript_never_retires_whatever_the_sidecar_says(
    tmp_path: Path, provider_registry,
) -> None:
    quest = _seed_single_agent(tmp_path)
    tdir = _seed_transcript(quest, "codex", body="")
    _mark_in_flight(quest, tdir)
    seen: list[dict[str, Any]] = []
    _register(provider_registry, quest, seen)

    await run_quest(quest)

    assert seen[0]["stashes"] == []
    assert _SWITCH_SENTENCE not in seen[0]["first_message"]


@pytest.mark.anyio
async def test_a_completed_step_re_dispatched_re_runs_clean(
    tmp_path: Path, provider_registry,
) -> None:
    # The one exemption: a rework Goto re-runs a step that FINISHED (current_step
    # cleared), so its continuity is the feedback it carries, not a seed.
    quest = _seed_single_agent(tmp_path)
    _seed_transcript(quest, "codex")
    seen: list[dict[str, Any]] = []
    _register(provider_registry, quest, seen)

    await run_quest(quest)

    snap = seen[0]
    assert snap["stashes"] == []
    assert snap["live_transcript"] == ""        # wiped by the fresh path, as today
    assert _SWITCH_SENTENCE not in snap["first_message"]
    assert snap["reincarnating"] == []


@pytest.mark.anyio
async def test_limb_2_requires_current_step_to_name_this_steps_directory(
    tmp_path: Path, provider_registry,
) -> None:
    # A paused step A followed by a Goto to a different step must not stash the
    # dispatched step's transcript on A's account.
    quest = _seed_single_agent(tmp_path)
    _seed_transcript(quest, "codex")
    _mark_in_flight(quest, quest.quest_dir / "steps" / "only" / "other")
    seen: list[dict[str, Any]] = []
    _register(provider_registry, quest, seen)

    await run_quest(quest)

    assert seen[0]["stashes"] == []
    assert _SWITCH_SENTENCE not in seen[0]["first_message"]


@pytest.mark.anyio
async def test_a_cross_vendor_model_override_is_retired_not_replayed(
    tmp_path: Path, provider_registry,
) -> None:
    # The pre-existing crash: today this reaches restore_rollout against a
    # thread.json the other vendor never wrote. The guard asks the same question
    # the dispatcher does — current_provider_model, which prefers the override.
    quest = _seed_single_agent(tmp_path)
    _seed_transcript(quest, "fake")
    state = State.load(quest.state_dir)
    # Seed the tier map too: _seed_single_agent leaves resolved_tiers None, so
    # did_fresh_resolve would be True and the fresh resolve at
    # `src/imp/engine/executor.py:491-495` would overwrite resolved_steps with
    # {} before the guard ever runs — the override has to survive to be tested.
    state.resolved_tiers = {t: ProviderModel(provider="fake", model="m")
                            for t in ("low", "medium", "high")}
    state.resolved_steps = {"w": ProviderModel(provider="codex", model="m")}
    state.resume_pending = state.step
    state.save(quest.state_dir)
    seen: list[dict[str, Any]] = []
    _register(provider_registry, quest, seen, names=("fake", "codex"))

    await run_quest(quest)

    assert seen[0]["stashes"] == ["w.attempt-1"]
    assert _SWITCH_SENTENCE in seen[0]["first_message"]


@pytest.mark.anyio
async def test_the_live_note_slot_is_emptied_into_the_stash(
    tmp_path: Path, provider_registry,
) -> None:
    quest = _seed_single_agent(tmp_path)
    tdir = _seed_transcript(quest, "codex")
    handoff_path_for(tdir).write_text("a previous generation's note")
    _arm_resume(quest)
    seen: list[dict[str, Any]] = []
    _register(provider_registry, quest, seen)

    await run_quest(quest)

    assert seen[0]["live_note"] is None
    assert seen[0]["stashed_note"] == ["a previous generation's note"]


@pytest.mark.anyio
async def test_the_guard_emits_exactly_one_reincarnating_event(
    tmp_path: Path, provider_registry,
) -> None:
    # Never `handoff failed`, even though the note was moved into the stash.
    quest = _seed_single_agent(tmp_path)
    _seed_transcript(quest, "codex")
    _arm_resume(quest)
    seen: list[dict[str, Any]] = []
    _register(provider_registry, quest, seen)

    await run_quest(quest)

    assert seen[0]["reincarnating"] == ["provider switched"]


@pytest.mark.anyio
async def test_a_switch_does_not_spend_reincarnation_budget(
    tmp_path: Path, provider_registry,
) -> None:
    quest = _seed_single_agent(tmp_path)
    _seed_transcript(quest, "codex")
    _arm_resume(quest)
    seen: list[dict[str, Any]] = []
    _register(provider_registry, quest, seen)

    await run_quest(quest)

    assert seen[0]["reincarnations"] == 0


@pytest.mark.anyio
async def test_a_same_name_pin_leaves_the_transcript_unstashed(
    tmp_path: Path, provider_registry,
) -> None:
    # AC 7(e)'s second half: pinning the vendor already resolved resumes
    # normally — the reconcile no-ops (Task 4 pins that) and the guard finds a
    # matching owner, so the step's transcript survives to be replayed rather
    # than being stashed over a request that changes nothing.
    quest = _seed_single_agent(tmp_path)
    _seed_transcript(quest, "fake", body="work worth keeping")
    _arm_resume(quest)
    quest.set_provider("fake")          # the vendor this run already resolves
    seen: list[dict[str, Any]] = []
    _register(provider_registry, quest, seen)

    assert await run_quest(quest) is RunResult.COMPLETED

    snap = seen[0]
    assert snap["stashes"] == []
    assert "work worth keeping" in snap["live_transcript"]
    assert _SWITCH_SENTENCE not in snap["first_message"]
    assert snap["reincarnating"] == []


@pytest.mark.anyio
async def test_a_pin_applied_at_entry_retires_at_the_dispatch_that_follows(
    tmp_path: Path, floor_availability,
) -> None:
    # The cross-process headline (AC 13/14): a step parked on claude_code,
    # switched to codex by another process, then resumed. Reconcile (a) re-points
    # the map at entry and the guard retires at the dispatch — each half is
    # proven above; this is their composition, which is what the user lives.
    # Its (c) clause — the following step dispatched normally — rides here too.
    from tests.test_executor import _seed_minimal_workflow
    from tests.test_executor_provider_pin import _floor_config

    quest, _phase, _step = _seed_minimal_workflow(tmp_path)
    _floor_config(tmp_path)
    # Two steps, so the dispatch AFTER the retirement can be observed too.
    (tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml").write_text(
        "name: t\ndescription: t\n"
        "defaults: {max_retries: 1, max_loops: 6}\n"
        "phases:\n"
        "  - name: only\n"
        "    steps:\n"
        "      - {name: w, file: agents/writer.md, tier: low}\n"
        "      - {name: w2, file: agents/writer.md, tier: low}\n")
    tdir = quest.quest_dir / "steps" / "only" / "w"
    tdir.mkdir(parents=True, exist_ok=True)
    Transcript(tdir / "transcript.jsonl").append(
        Message(role="assistant", content=(TextBlock(text="work from the fled vendor"),)))
    write_transcript_owner(tdir, "claude_code")
    quest.event_log().append(
        agent_id="step:only/w", type="engine_message",
        payload={"role": "system", "subtype": "system_prompt",
                 "content": [{"type": "text", "text": "stored"}]})
    state = State.load(quest.state_dir)
    state.resolved_tiers = {
        t: ProviderModel(provider="claude_code", model=mo)
        for t, mo in BUILTIN_FLOOR["claude_code"].tier_defaults.items()}
    state.resume_pending = state.step
    state.save(quest.state_dir)
    quest.set_provider("codex")          # e.g. /provider at a parked hold prompt

    seen: list[dict[str, Any]] = []

    class _Runner:
        # Public attribute only — see tests/test_no_private_reachthroughs.py.
        def __init__(self, model: str) -> None:
            self.model = model

        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            stashes = sorted((quest.quest_dir / "steps" / "only").glob("w.attempt-*"))
            seen.append({
                "model": self.model,
                "stashes": [p.name for p in stashes],
                "stashed": "".join(
                    (p / "transcript.jsonl").read_text() for p in stashes),
                "first": (inv.initial_messages[0].content[0].text
                          if inv.initial_messages else ""),
            })
            yield {"type": "agent_result",
                   "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "x"

        def __init__(self, model: str) -> None:
            self.model = model

        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner(self.model)

    floor_availability({"claude_code", "codex"}, factory=lambda mo: _P(mo))

    assert await run_quest(quest, on_event=lambda _e: None) is RunResult.COMPLETED

    snap = seen[0]
    assert snap["model"] == BUILTIN_FLOOR["codex"].tier_defaults["low"]
    assert snap["stashes"] == ["w.attempt-1"]
    assert "work from the fled vendor" in snap["stashed"]   # stashed, not wiped
    assert _SWITCH_SENTENCE in snap["first"]
    # AC 13(c): the NEXT step runs on the new vendor and is dispatched normally
    # — the retirement's seed belongs to that one entry and never leaks forward.
    assert seen[1]["model"] == BUILTIN_FLOOR["codex"].tier_defaults["low"]
    assert _SWITCH_SENTENCE not in seen[1]["first"]


@pytest.mark.anyio
async def test_the_reborn_session_records_the_vendor_that_now_owns_it(
    tmp_path: Path, provider_registry,
) -> None:
    # So a second consecutive switch retires again and a resume without one
    # does not.
    quest = _seed_single_agent(tmp_path)
    _seed_transcript(quest, "codex")
    _arm_resume(quest)
    seen: list[dict[str, Any]] = []
    _register(provider_registry, quest, seen)

    await run_quest(quest)

    assert seen[0]["live_sidecar"] == "fake"


@pytest.mark.anyio
async def test_the_clean_tree_bounce_re_dispatch_retires_a_foreign_transcript(
    tmp_path: Path, floor_availability,
) -> None:
    # AC 9d(a)/(b), the scenario an earlier design got wrong: _enforce_clean_tree
    # clears current_step at `src/imp/engine/executor.py:1536` *and* returns
    # Goto(target=step.name, resume=True) at `:1537`, so the bounce arms a resume
    # over a transcript the fled vendor wrote. Keying retirement at the dispatch
    # rather than at the switch is what catches it: the bounce re-dispatch is an
    # ordinary trip through the step loop, so it crosses reconcile (b) and then
    # the guard. Driven end to end through a real dirty worktree rather than by
    # calling _enforce_clean_tree directly, since the claim is about the path.
    _seed_control(tmp_path)
    (project_state_dir(tmp_path) / "config.yaml").write_text("providers: [claude_code]\n")
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    state = State.load(quest.state_dir)
    state.resolved_tiers = {
        t: ProviderModel(provider="claude_code", model=mo)
        for t, mo in BUILTIN_FLOOR["claude_code"].tier_defaults.items()}
    state.save(quest.state_dir)
    tdir = quest.quest_dir / "steps" / "p" / "s"
    dirt = quest.project_root / "scratch.txt"
    seen: list[dict[str, Any]] = []

    class _Runner:
        # Public attribute only — see tests/test_no_private_reachthroughs.py.
        def __init__(self, model: str) -> None:
            self.model = model

        async def run(self, inv: AgentInvocation) -> AsyncIterator[Event]:
            n = len(seen) + 1
            stashes = sorted((quest.quest_dir / "steps" / "p").glob("s.attempt-*"))
            seen.append({
                "model": self.model,
                "stashes": [p.name for p in stashes],
                "stashed": "".join(
                    (p / "transcript.jsonl").read_text() for p in stashes),
                "first": (inv.initial_messages[0].content[0].text
                          if inv.initial_messages else ""),
            })
            if n == 1:
                # A real provider persists its turns; the guard reads that file,
                # so the fake has to leave one behind for there to be anything
                # the bounce could replay into the wrong vendor.
                Transcript(tdir / "transcript.jsonl").append(
                    Message(role="assistant",
                            content=(TextBlock(text="claude_code turn"),)))
                # A real run always journals this beside the transcript, and the
                # resume path's invariant check requires it; without it the
                # replay dies on that instead of on the vendor mismatch.
                quest.event_log().append(
                    agent_id="step:p/s", type="engine_message",
                    payload={"role": "system", "subtype": "system_prompt",
                             "content": [{"type": "text", "text": "stored"}]})
                dirt.write_text("uncommitted\n")   # the agent leaves the tree dirty
                quest.set_provider("codex")        # the daemon arms the switch mid-step
            else:
                dirt.unlink()                      # cleaned, so the bounce settles
            yield {"type": "agent_result",
                   "payload": {"outcome": "ok", "status": "Success"}}

        async def interrupt(self) -> None:
            pass

    class _P:
        name = "x"

        def __init__(self, model: str) -> None:
            self.model = model

        def create_runner(self, _i: AgentInvocation) -> AgentRunner:
            return _Runner(self.model)

    floor_availability({"claude_code", "codex"}, factory=lambda mo: _P(mo))
    events: list[dict[str, Any]] = []

    assert await run_quest(quest, on_event=events.append) is RunResult.COMPLETED

    # AC 9d(c): the bounce re-runs the step, and _enforce_clean_tree already cleared current_step.
    lines = [e["payload"]["stdout"] for e in events if e["type"] == "script_output"]
    assert "provider switched — codex picks up this step from the event log" in lines
    assert "provider switched — codex takes the next step" not in lines
    first, bounced = seen
    assert first["model"] == BUILTIN_FLOOR["claude_code"].tier_defaults["medium"]
    # The bounce re-dispatch runs on the new vendor, and the guard retired the
    # old one's transcript at entry: stashed rather than replayed, so `keep` and
    # `balance_on_resume` are both False and restore_rollout is unreachable.
    assert bounced["model"] == BUILTIN_FLOOR["codex"].tier_defaults["medium"]
    assert bounced["stashes"] == ["s.attempt-1"]
    assert "claude_code turn" in bounced["stashed"]
    assert _SWITCH_SENTENCE in bounced["first"]
    # The bounce's own feedback still reaches the reborn session — retiring the
    # transcript does not cost the step the reason it was bounced.
    assert "uncommitted changes" in bounced["first"]
