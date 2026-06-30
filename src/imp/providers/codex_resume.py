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


def derive_codex_resume_state(
    events_path: Path, agent_id: str, *, subagent_depth: int = 0,
) -> tuple[ResumeState, str]:
    """Return (state, last_agent_text). last_agent_text is the tail
    agent_message text (needed by the resume seam, since proc_state is empty on
    a fresh resume process)."""
    if not events_path.exists():
        return ResumeState.FRESH, ""
    records = []
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
    last_llm = None
    for rec in records:
        if rec.get("type") == "llm_response":
            last_llm = rec
    if has_complete_step or last_llm is None:
        return ResumeState.SEND_TO_LLM, ""
    has_awaiting = any(
        rec.get("type") == "awaiting_user_input" for rec in records)
    last_text = _text_of(last_llm)
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
