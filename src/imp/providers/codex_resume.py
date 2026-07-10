# src/imp/providers/codex_resume.py
"""Resume-state derivation for codex from imp's own events.jsonl.

codex resumes via its opaque native rollout (codex_rollout.py), so imp holds no
parsed message list and cannot run agents.classify.classify_resume. But imp owns
events.jsonl, which carries the same signal: a turn ended awaiting the user iff
the agent emitted an llm_response but NO complete_step tool_call (presence-based,
position-independent — see the Unit F/G coupling note). Mirrors classify_resume's
AWAITING_USER/MISSING split. DANGLING_TOOL_USE is omitted: codex always emits paired
tool_call/tool_result events, so a dangling tool_call cannot appear in events.jsonl.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from imp.agents.classify import ResumeState


def _read_agent_records(events_path: Path, agent_id: str) -> list[dict[str, Any]]:
    """Filtered, order-preserving list of this agent's events.jsonl records."""
    records: list[dict[str, Any]] = []
    for line in events_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("agent_id") == agent_id:
            records.append(rec)
    return records


def derive_codex_resume_state(
    events_path: Path, agent_id: str, *, subagent_depth: int = 0,
) -> tuple[ResumeState, str]:
    """Return (state, last_agent_text). last_agent_text is the tail
    agent_message text (needed by the resume seam, since proc_state is empty on
    a fresh resume process)."""
    if not events_path.exists():
        return ResumeState.FRESH, ""
    records = _read_agent_records(events_path, agent_id)
    if not records:
        return ResumeState.FRESH, ""

    # Presence-based (position-independent): complete_step fires at most once per
    # step (it terminates the turn). If it's anywhere in this agent's records the
    # step completed → resume normally; F1's emit-order change can't break this.
    # Scanning the full agent_id history is safe: this helper is called once at
    # resume time over a SINGLE prior run's events; complete_step terminates that
    # run, so an early complete_step can't mask a later awaiting-user turn.
    has_complete_step = any(
        rec.get("type") == "tool_call"
        and rec.get("payload", {}).get("name") == "complete_step"
        for rec in records
    )
    # Read the most recent PROSE-BEARING llm_response (raw.content has a
    # non-empty text block). A prose-less tool-only turn emits an empty-prose
    # llm_response at turn.completed purely as the usage carrier; that carrier
    # must not shadow the turn's real prose with "". The `is None` gate keys on
    # this same lookup: no prose-bearing response anywhere -> nothing to replay
    # -> SEND_TO_LLM (subsumes the old empty-history case).
    last_prose_llm = None
    for rec in records:
        if rec.get("type") == "llm_response" and _text_of(rec):
            last_prose_llm = rec
    if has_complete_step or last_prose_llm is None:
        return ResumeState.SEND_TO_LLM, ""
    has_awaiting = any(
        rec.get("type") == "awaiting_user_input" for rec in records)
    last_text = _text_of(last_prose_llm)
    if has_awaiting:
        return ResumeState.AWAITING_USER, last_text
    if subagent_depth > 0:
        # A subagent never calls complete_step; its bare-turn-end terminal IS
        # the done-signal. Resume normally with no continue-nudge — the live
        # bare-turn-end arm re-emits the deliverable. Top-level keeps the
        # MISSING_COMPLETE_STEP replay-and-continue below.
        return ResumeState.SEND_TO_LLM, last_text
    return ResumeState.MISSING_COMPLETE_STEP, last_text


def _text_of(llm_record: dict[str, Any]) -> str:
    content = llm_record.get("payload", {}).get("raw", {}).get("content", [])
    return " ".join(
        b.get("text", "") for b in content if b.get("type") == "text").strip()


def _user_text_of(rec: dict[str, Any]) -> str | None:
    """Join the type=='text' content blocks of a user_message event; None if
    the joined text is empty/whitespace."""
    content = rec.get("payload", {}).get("content", [])
    text = " ".join(
        b.get("text", "") for b in content if b.get("type") == "text").strip()
    return text or None


def pending_human_reply(events_path: Path, agent_id: str) -> str | None:
    """Recover an un-answered, already-solicited human reply from events.jsonl,
    or None. Anchored on the LAST awaiting_user_input gate: scan forward from
    that gate; a user_message sets the candidate, an llm_response or tool_call
    clears it (the agent already acted on the reply)."""
    if not events_path.exists():
        return None
    records = _read_agent_records(events_path, agent_id)
    last_gate = None
    for i, rec in enumerate(records):
        if rec.get("type") == "awaiting_user_input":
            last_gate = i
    if last_gate is None:
        return None
    candidate: str | None = None
    for rec in records[last_gate:]:
        rtype = rec.get("type")
        if rtype == "user_message":
            candidate = _user_text_of(rec)
        elif rtype in ("llm_response", "tool_call"):
            candidate = None
    return candidate
