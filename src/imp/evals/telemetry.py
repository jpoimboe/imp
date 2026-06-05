"""Read per-agent telemetry back from a run's events.jsonl (spec Harness §,
Telemetry). Tokens from `llm_response.usage`, tool-call count from `tool_call`
events, wall-clock from the min/max event timestamp — all filtered by agent_id so
panel members (distinct agent_ids) aggregate independently.

LOAD-BEARING INVARIANT: each `llm_response`/`tool_call` appears in events.jsonl
EXACTLY ONCE. The production runners (anthropic/openai/claude_code) write these
DIRECTLY via `elog.append_*` and only *yield* `error`/`agent_output`; meanwhile
`drive_invocation(emit_events=True)` appends every YIELDED event. So the two paths
don't overlap and counts aren't doubled. If a future provider ever *yields* an
`llm_response`/`tool_call` (instead of appending it), `emit_events=True` would
silently DOUBLE every token/tool count -- and thus every Q3 dollar figure. A run
with suspiciously 2x telemetry is the symptom to check here first."""

from __future__ import annotations

import contextlib
from datetime import datetime
from pathlib import Path
from typing import Any

from imp.engine.state import JsonlEventLog
from imp.evals.models import Telemetry


def _parse_ts(value: str) -> datetime:
    # events store datetime.now(UTC).isoformat(); tolerate a trailing Z.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _normalize_usage(
    usage: dict[str, Any], provider: str | None = None
) -> tuple[int, int, int, int]:
    """Return (fresh_input, output, cache_read, cache_creation), normalizing the
    two provider shapes so Q3's $-comparison is apples-to-apples (B4):
    - Anthropic: `input_tokens` EXCLUDES cache; cache in
      `cache_read_input_tokens` / `cache_creation_input_tokens`.
    - OpenAI: `input_tokens` is cache-INCLUSIVE; cached count is nested under
      `input_tokens_details.cached_tokens` (openai.py:_usage_dict). Pricing the
      full input at the fresh rate overstates OpenAI cost by a large factor on a
      heavily-cached reviewer prompt.
    `provider` (from the event payload) is authoritative when present; the
    key-shape heuristic is only a fallback. NOTE: OpenAI's `_usage_dict` FALLBACK
    path (openai.py:254-256) drops `cached_tokens` entirely -- then cache_read
    reads 0 and that run's OpenAI cost is over-stated; acceptable (conservative)
    and flagged."""
    out = int(usage.get("output_tokens", 0) or 0)
    details = usage.get("input_tokens_details")
    is_openai = (provider == "openai") or (
        provider is None and (details is not None or "cached_tokens" in usage)
    )
    if is_openai:
        cached = int((details or {}).get("cached_tokens", usage.get("cached_tokens", 0)) or 0)
        total_in = int(usage.get("input_tokens", 0) or 0)
        return max(0, total_in - cached), out, cached, 0
    # Anthropic-style (or a cache-less plain shape).
    fresh = int(usage.get("input_tokens", 0) or 0)
    cr = int(usage.get("cache_read_input_tokens", 0) or 0)
    cc = int(usage.get("cache_creation_input_tokens", 0) or 0)
    return fresh, out, cr, cc


def read_telemetry(events_path: Path, agent_id: str) -> Telemetry:
    if not events_path.exists():
        return Telemetry.zero()
    log = JsonlEventLog(events_path)
    inp = out = cr = cc = tool_calls = 0
    auth_mode: str | None = None
    timestamps: list[datetime] = []
    for ev in log.read():
        if ev.get("agent_id") != agent_id:
            continue
        ts = ev.get("timestamp")
        if isinstance(ts, str):
            with contextlib.suppress(ValueError):
                timestamps.append(_parse_ts(ts))
        etype = ev.get("type")
        payload = ev.get("payload") or {}
        if etype == "llm_response":
            f_in, f_out, f_cr, f_cc = _normalize_usage(
                payload.get("usage") or {}, payload.get("provider")
            )
            inp += f_in
            out += f_out
            cr += f_cr
            cc += f_cc
            # Codex runs stamp `auth_mode` onto every llm_response payload.
            # The last-seen value wins (a run never switches mode mid-flight,
            # so any non-None value is canonical for the agent's whole run).
            am = payload.get("auth_mode")
            if isinstance(am, str):
                auth_mode = am
        elif etype == "llm_response_usage_patch":
            # Fold patch into the running totals; the matching llm_response was
            # already counted above. No new response counted.
            u = payload.get("usage") or {}
            f_in, f_out, f_cr, f_cc = _normalize_usage(u, provider=None)
            inp += f_in
            out += f_out
            cr += f_cr
            cc += f_cc
        elif etype == "tool_call":
            tool_calls += 1
    wall = (max(timestamps) - min(timestamps)).total_seconds() if len(timestamps) >= 2 else 0.0
    return Telemetry(
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cr,
        cache_creation_tokens=cc,
        tool_calls=tool_calls,
        wall_seconds=wall,
        auth_mode=auth_mode,
    )
