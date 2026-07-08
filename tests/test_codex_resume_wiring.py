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
            title="t", project_root=tmp_path,
            workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx",
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


# --- C1: event-derived AWAITING_USER for a paused wait_for_user -------------
# Codex holds no parsed message list; it reconstructs resume state from imp's
# events.jsonl. A wait_for_user pause journals an `awaiting_user_input` event
# (emitted by the tool before the headless re-raise) with no following
# `complete_step`, which derives ResumeState.AWAITING_USER.

from imp.agents.classify import ResumeState  # noqa: E402
from imp.providers.codex_resume import (  # noqa: E402
    derive_codex_resume_state,
)


def _write_events(p: Path, records: list[dict]) -> None:
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_derive_awaiting_user_when_awaiting_event_present(tmp_path: Path) -> None:
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "step:t", "type": "llm_response",
         "payload": {"raw": {"content": [{"type": "text", "text": "merge?"}]}}},
        {"agent_id": "step:t", "type": "awaiting_user_input",
         "payload": {"prompt": "merge?"}},
    ])
    state, last_text = derive_codex_resume_state(ev, "step:t")
    assert state is ResumeState.AWAITING_USER
    assert last_text == "merge?"


def test_derive_missing_complete_step_without_awaiting(tmp_path: Path) -> None:
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "step:t", "type": "llm_response",
         "payload": {"raw": {"content": [{"type": "text", "text": "done?"}]}}},
    ])
    state, _ = derive_codex_resume_state(ev, "step:t")
    assert state is ResumeState.MISSING_COMPLETE_STEP


def test_derive_send_to_llm_when_complete_step_present(tmp_path: Path) -> None:
    # complete_step present → the turn completed; resume normally even though an
    # earlier awaiting_user_input exists (presence-of-complete wins).
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "step:t", "type": "awaiting_user_input",
         "payload": {"prompt": "merge?"}},
        {"agent_id": "step:t", "type": "tool_call",
         "payload": {"name": "complete_step", "input": {}}},
        {"agent_id": "step:t", "type": "llm_response",
         "payload": {"raw": {"content": [{"type": "text", "text": "answer"}]}}},
    ])
    state, _ = derive_codex_resume_state(ev, "step:t")
    assert state is ResumeState.SEND_TO_LLM


def test_derive_skips_trailing_empty_prose_usage_carrier(tmp_path: Path) -> None:
    # A prose turn, then a prose-less turn whose only llm_response is the empty-
    # prose usage carrier. last_text must come from the prose turn, not "".
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "step:t", "type": "llm_response",
         "payload": {"raw": {"content": [{"type": "text", "text": "which file?"}]}}},
        {"agent_id": "step:t", "type": "awaiting_user_input",
         "payload": {"prompt": "which file?"}},
        {"agent_id": "step:t", "type": "llm_response",
         "payload": {"raw": {"content": []}}},   # empty-prose usage carrier
    ])
    state, last_text = derive_codex_resume_state(ev, "step:t")
    assert state is ResumeState.AWAITING_USER
    assert last_text == "which file?"


def test_derive_send_to_llm_when_only_empty_prose_carriers(tmp_path: Path) -> None:
    # A run whose only llm_responses are empty-prose usage carriers (all turns
    # tool-only, no agent_message) has no prose to replay -> SEND_TO_LLM.
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "step:t", "type": "tool_call",
         "payload": {"name": "write_file", "input": {}}},
        {"agent_id": "step:t", "type": "llm_response",
         "payload": {"raw": {"content": []}}},
    ])
    state, last_text = derive_codex_resume_state(ev, "step:t")
    assert state is ResumeState.SEND_TO_LLM
    assert last_text == ""


def test_derive_preamble_only_turn_recovers_last_text(tmp_path: Path) -> None:
    # A preamble turn whose prose leads its tool_call, followed by a prose-less
    # continuation whose only llm_response is an empty-prose usage carrier.
    # Under old code the trailing carrier is last_llm → last_text == ""; under
    # new code the prose-bearing lookup skips it and recovers "drafting the fix".
    ev = tmp_path / "events.jsonl"
    _write_events(ev, [
        {"agent_id": "step:t", "type": "llm_response",
         "payload": {"raw": {"content": [{"type": "text", "text": "drafting the fix"}]}}},
        {"agent_id": "step:t", "type": "tool_call",
         "payload": {"name": "write_file", "input": {}}},
        {"agent_id": "step:t", "type": "tool_result",
         "payload": {"tool_use_id": "c1", "content": "ok"}},
        {"agent_id": "step:t", "type": "llm_response",
         "payload": {"raw": {"content": []}}},   # trailing empty-prose carrier
    ])
    state, last_text = derive_codex_resume_state(ev, "step:t")
    assert state is ResumeState.MISSING_COMPLETE_STEP
    assert last_text == "drafting the fix"


@pytest.mark.anyio
async def test_resume_missing_complete_step_continues_with_nudge(tmp_path: Path) -> None:
    from imp.agents.transcript import RESUME_CONTINUE_NUDGE
    _seed_git_and_auth(tmp_path)
    tx = tmp_path / "tx"
    rel = "sessions/2026/06/05/rollout-2026-06-05T00-00-00-thr-cont-1.jsonl"
    (tx / "codex_thread.json").write_text(
        json.dumps({"thread_id": "thr-cont-1", "rollout_relpath": rel}))
    saved = tx / "codex_rollout" / rel
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text('{"id": "thr-cont-1"}\n')

    # events.jsonl that derives MISSING_COMPLETE_STEP (llm_response, no complete_step).
    _write_events(tmp_path / "events.jsonl", [
        {"agent_id": "step:t", "type": "llm_response",
         "payload": {"raw": {"content": [{"type": "text", "text": "stopped before finishing"}]}}},
    ])

    elog = InMemoryEventLog()
    scripted = [[
        thread_started("thr-cont-1"),
        agent_message("resumed; calling complete_step"),
        mcp_call(name="complete_step", args={"answer": "42"}, result="ok", item_id="cs-1"),
        turn_completed(input_tokens=1, output_tokens=1),
    ]]
    fake = FakeCodex(scripted_turns=scripted)
    runner = CodexAgentRunner(
        model_name="gpt-5.5", client_factory=lambda options=None: fake)
    runner._canonical_auth_override = tmp_path / "auth.json"

    inv = _build_invocation(tmp_path, event_log=elog, balance_on_resume=True)
    events = [ev async for ev in runner.run(inv)]

    # No missing_complete_step error; the run continued and completed.
    assert not any(e.get("type") == "error"
                   and e["payload"].get("kind") == "missing_complete_step"
                   for e in events)
    assert any(e.get("type") == "agent_output" for e in events)
    # The continue nudge was journaled as the resumed user message.
    assert any(e.get("type") == "user_message"
               and RESUME_CONTINUE_NUDGE in json.dumps(e.get("payload", {}),
                                                       ensure_ascii=False)
               for e in elog.read())


@pytest.mark.anyio
async def test_awaiting_user_retry_continue_journals_engine_continuation_not_user_message(
    tmp_path: Path,
) -> None:
    """codex retry-continue of a bare-turn-end AWAITING_USER (derived from
    events.jsonl): the resume branch emits NO awaiting_user_input, journals the
    nudge as an internal engine_continuation event (not a user_message), and
    still drives the resumed prompt with the nudge."""
    from imp.engine.agent_run import _CONTINUATION_NUDGE

    _seed_git_and_auth(tmp_path)
    tx = tmp_path / "tx"
    rel = "sessions/2026/06/05/rollout-2026-06-05T00-00-00-thr-rc-1.jsonl"
    (tx / "codex_thread.json").write_text(
        json.dumps({"thread_id": "thr-rc-1", "rollout_relpath": rel}))
    saved = tx / "codex_rollout" / rel
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text('{"id": "thr-rc-1"}\n')

    # events.jsonl deriving AWAITING_USER: a tail llm_response + the journaled
    # awaiting_user_input, with no complete_step.
    _write_events(tmp_path / "events.jsonl", [
        {"agent_id": "step:t", "type": "llm_response",
         "payload": {"raw": {"content": [{"type": "text", "text": "which file?"}]}}},
        {"agent_id": "step:t", "type": "awaiting_user_input",
         "payload": {"prompt": "which file?"}},
    ])

    elog = InMemoryEventLog()
    scripted = [[
        thread_started("thr-rc-1"),
        agent_message("resumed; calling complete_step"),
        mcp_call(name="complete_step", args={"answer": "42"},
                 result="ok", item_id="cs-1"),
        turn_completed(input_tokens=1, output_tokens=1),
    ]]
    fake = FakeCodex(scripted_turns=scripted)
    runner = CodexAgentRunner(
        model_name="gpt-5.5", client_factory=lambda options=None: fake)
    runner._canonical_auth_override = tmp_path / "auth.json"

    def _must_not_solicit(_prompt):
        raise AssertionError("retry-continue must not solicit the human")

    inv = _build_invocation(tmp_path, event_log=elog, balance_on_resume=True)
    object.__setattr__(inv, "resume_should_continue", True)
    object.__setattr__(inv, "user_input_fn", _must_not_solicit)

    events = [ev async for ev in runner.run(inv)]

    types = [e["type"] for e in elog.read()]
    assert "engine_continuation" in types
    # The resume branch no longer emits awaiting_user_input to the event log
    # (the seeded one is on-disk in events.jsonl, a separate file the classifier
    # reads; elog is the fresh in-memory log the run writes to).
    assert "awaiting_user_input" not in types
    # Defect B: the nudge is NOT journaled as a user_message.
    assert not any(
        e["type"] == "user_message"
        and _CONTINUATION_NUDGE in json.dumps(e.get("payload", {}),
                                              ensure_ascii=False)
        for e in elog.read())
    cont = next(e for e in elog.read() if e["type"] == "engine_continuation")
    assert cont["payload"] == {"text": _CONTINUATION_NUDGE}
    # The resumed prompt is the nudge, so the run continues and completes.
    assert _CONTINUATION_NUDGE in str(fake.thread.run_calls[0][0])
    assert any(e.get("type") == "agent_output" for e in events)
