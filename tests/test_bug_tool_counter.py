# tests/test_bug_tool_counter.py
"""Regression repro for spec §8: the status-line tool counter is stuck at
`1 tools` no matter how many tools run consecutively.

Root cause (confirmed against a real claude_code run): the provider emits an
empty-text `llm_response` — a lone `tool_use` block, so `AgentTextNode.text`
is empty — between consecutive `tool_call`s. `_h_llm_response` (and `_h_thinking`)
call `_flush_burst()` UNCONDITIONALLY, nulling `_current_burst` before the next
tool starts, so every tool begins a fresh burst with count 1.

The fixture is a trimmed, real slice of the 2026-06-03-imp-new-open-by-asking
run (spec-writer making seven consecutive bash/grep/read calls). String payloads
were truncated for size; content length does not affect the burst counter.

This test FAILS today (counter never exceeds 1) and should PASS once the flush
no longer fires on an empty-text tool-use-turn carrier. It is marked
`xfail(strict=True)` so the suite stays green now and the marker must be removed
when the §8 fix lands (the fix turns the xfail into an xpass → strict failure).
"""
from __future__ import annotations

import json
import re

import pytest

from imp.cli import narrative as nv
from tests.conftest import FIXTURES

_FIXTURE = FIXTURES / "cc_tool_counter_burst.jsonl"


def _events() -> list[dict]:
    return [json.loads(ln) for ln in _FIXTURE.read_text().splitlines() if ln.strip()]


@pytest.mark.xfail(strict=True, reason="spec §8: tool counter stuck at 1 (unfixed)")
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
    # should climb well above 1. Today it is stuck at 1 (the bug).
    assert max_count > 1, (
        f"tool counter stuck at {max_count}; consecutive tool calls are not "
        f"accumulating into one burst (empty-text llm_response flushes between them)"
    )
