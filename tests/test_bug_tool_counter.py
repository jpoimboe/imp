# tests/test_bug_tool_counter.py
"""Regression guard for spec §8: the status-line tool counter must accumulate
across consecutive tool calls within a single burst.

Root cause that was fixed in §8: the provider emits an empty-text
`llm_response` — a lone `tool_use` block, so `AgentTextNode.text` is empty —
between consecutive `tool_call`s. Before the fix, `_h_llm_response` (and
`_h_thinking`) called `_flush_burst()` unconditionally, nulling `_current_burst`
before the next tool started, so every tool began a fresh burst with count 1.
The fix gates the flush on non-empty text, so consecutive tool calls now
accumulate into a single burst and the counter climbs correctly.

The fixture is a trimmed, real slice of the 2026-06-03-imp-new-open-by-asking
run (spec-writer making seven consecutive bash/grep/read calls). String payloads
were truncated for size; content length does not affect the burst counter.
"""
from __future__ import annotations

import json
import re

from imp.cli import narrative as nv
from tests.conftest import FIXTURES

_FIXTURE = FIXTURES / "cc_tool_counter_burst.jsonl"


def _events() -> list[dict]:
    return [json.loads(ln) for ln in _FIXTURE.read_text().splitlines() if ln.strip()]


def test_consecutive_tools_accumulate_into_one_burst() -> None:
    builder = nv.NarrativeBuilder()
    max_count = 0
    for event in _events():
        builder.add(event)
        if event.get("type") == "tool_call":
            activity = builder.current_activity() or ""
            m = re.search(r"·\s*(\d+)\s*tools?", activity)
            if m:
                max_count = max(max_count, int(m.group(1)))

    # The fixture contains a run of seven consecutive tool calls; the counter
    # must climb above 1 as they accumulate into a single burst.
    assert max_count > 1, (
        f"tool counter stuck at {max_count}; consecutive tool calls are not "
        f"accumulating into one burst (empty-text llm_response flushes between them)"
    )
