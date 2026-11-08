# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
# src/imp/providers/codex_resume.py
"""Resume-state derivation for codex from imp's own events.jsonl.

codex resumes via its opaque native rollout (codex_rollout.py), so imp holds no
parsed message list and cannot run agents.classify.classify_resume. But imp owns
events.jsonl, which carries the same signal: a turn ended awaiting the user iff the
agent emitted an agent_message but NO complete_step tool_call — that split is
presence-based, position-independent (see the Unit F/G coupling note) — AND the last
awaiting_user_input gate has no tool_call after it (a tool_call after the gate means
the agent took the reply and went back to work). Mirrors classify_resume's
AWAITING_USER/MISSING split. DANGLING_TOOL_USE is omitted: a sub_agent tool_call opens
at item.started and closes at item.completed, so an interrupt between them would
dangle — codex_runner closes those folds at teardown, keeping events.jsonl paired.
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


def _last_gate_index(records: list[dict[str, Any]]) -> int | None:
    """Index of the LAST awaiting_user_input record, or None if there is none."""
    last = None
    for i, rec in enumerate(records):
        if rec.get("type") == "awaiting_user_input":
            last = i
    return last


def derive_codex_resume_state(
    events_path: Path, agent_id: str, *, subagent_depth: int = 0,
) -> tuple[ResumeState, str]:
    """Return (state, last_agent_text): the tail prose — for AWAITING_USER, the
    question the completion nudge followed — needed by the resume seam, since
    proc_state is empty on a fresh resume process."""
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
    # Read the most recent PROSE-BEARING agent_message (raw.content has a
    # non-empty text block). A prose-less tool-only turn emits an empty-prose
    # agent_message at turn.completed purely as the usage carrier; that carrier
    # must not shadow the turn's real prose with "". The `is None` gate keys on
    # this same lookup: no prose-bearing response anywhere -> nothing to replay
    # -> SEND_TO_LLM (subsumes the old empty-history case).
    last_prose = None
    for i, rec in enumerate(records):
        if _is_prose(rec):
            last_prose = i
    if has_complete_step or last_prose is None:
        return ResumeState.SEND_TO_LLM, ""
    # Anchored on the LAST gate, unlike the presence-based checks above: a
    # tool_call after it means the agent took the reply and went back to work,
    # so the turn was interrupted mid-work rather than yielding.
    last_gate = _last_gate_index(records)
    has_awaiting = last_gate is not None and not any(
        rec.get("type") == "tool_call" for rec in records[last_gate + 1:])
    if has_awaiting:
        return ResumeState.AWAITING_USER, _parked_question_text(records, last_prose)
    last_text = _text_of(records[last_prose])
    if subagent_depth > 0:
        # A subagent never calls complete_step; its bare-turn-end terminal IS
        # the done-signal. Resume normally with no continue-nudge — the live
        # bare-turn-end arm re-emits the deliverable. Top-level keeps the
        # MISSING_COMPLETE_STEP replay-and-continue below.
        return ResumeState.SEND_TO_LLM, last_text
    return ResumeState.MISSING_COMPLETE_STEP, last_text


def _text_of(rec: dict[str, Any]) -> str:
    content = rec.get("payload", {}).get("raw", {}).get("content", [])
    return " ".join(
        b.get("text", "") for b in content if b.get("type") == "text").strip()


def _is_prose(rec: dict[str, Any]) -> bool:
    return rec.get("type") == "agent_message" and bool(_text_of(rec))


def _is_completion_nudge(rec: dict[str, Any]) -> bool:
    return (rec.get("type") == "engine_message"
            and rec.get("payload", {}).get("subtype") == "completion")


def _parked_question_text(records: list[dict[str, Any]], last_prose: int) -> str:
    """The last prose, or — past a completion nudge — the nudged turn's prose ("" if none)."""
    prev = next((i for i in range(last_prose - 1, -1, -1)
                 if _is_prose(records[i]) or _is_completion_nudge(records[i])), None)
    if prev is None or _is_prose(records[prev]):
        return _text_of(records[last_prose])
    for j in range(prev - 1, -1, -1):
        if _is_prose(records[j]):
            return _text_of(records[j])
        if records[j].get("type") in ("user_message", "engine_message"):
            break
    return ""


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
    that gate; a user_message sets the candidate, an agent_message or tool_call
    clears it (the agent already acted on the reply)."""
    if not events_path.exists():
        return None
    records = _read_agent_records(events_path, agent_id)
    last_gate = _last_gate_index(records)
    if last_gate is None:
        return None
    candidate: str | None = None
    for rec in records[last_gate:]:
        rtype = rec.get("type")
        if rtype == "user_message" and \
                (rec.get("payload") or {}).get("subtype") in ("reply", "interject"):
            candidate = _user_text_of(rec)
        elif rtype in ("agent_message", "tool_call"):
            candidate = None
    return candidate
