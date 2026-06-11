"""imp-telegram — reference remote driver for imp (spec Part B).

Reads NDJSON events from imp on stdin, curates each into a Telegram message
(getUpdates is *outbound only* — no inbound port, tunnel, or hosting), and writes
chat replies back to stdout for imp's input-race to pick up. Ships out-of-core
under contrib/; its tests do not gate imp core.

Stdout is reserved for reply lines (one '\\n'-terminated line per reply); all
diagnostics go to stderr.
"""
from __future__ import annotations

from typing import Any

# Curation map (spec Decisions §"Default curation"). Curation lives in the
# driver, not in imp: imp only streams event *types*; the driver decides loud /
# silent / dropped.
_LOUD = frozenset({"awaiting_user_input", "errored", "merge_blocked", "completed"})
_SILENT = frozenset({"agent_output", "llm_response"})


def curate(event_type: Any) -> str | None:
    """Classify an imp event type: 'loud' (notify), 'silent' (quiet narrative),
    or None (dropped). Anything not loud/silent is dropped, including tool_call,
    tool_result, thinking, step_context and workflow_paused."""
    if event_type in _LOUD:
        return "loud"
    if event_type in _SILENT:
        return "silent"
    return None
