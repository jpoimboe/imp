# tests/test_bug_subagent_stall.py
"""Regression repro for spec §11: live output stalls partway through a run and
never recovers, even though `imp log` shows the full record.

Root cause (confirmed against a real claude_code run, NOT the human-gate
mechanism originally hypothesized): claude_code emits the `task` tool_call with
an EMPTY `payload['id']`. `_open_subagent_fold` keys the fold under that empty
block id; the closing `task` tool_result carries the real `toolu_...` id, so
`_h_tool_result` can never pop the fold and `SubagentFold.running` stays True
forever. The LiveDriver frontier cursor blocks behind the never-closing
container (`_container_closed` is False), so every finalized node after the
spawn is built into the tree but never emitted live. `imp log` renders the whole
tree with no live cursor, so it is unaffected.

The fixture is a trimmed, real slice of the 2026-06-03-imp-new-open-by-asking
run: the task-lead step spawns an `implementer` subagent (empty-id task call),
the subagent reports, the non-matching close arrives, then task-lead completes
and a `commit-check` step runs. The subagent collapses to a single status row
in lean/live (Part D); no internal prose is streamed. The parent step's
completion breadcrumb and the entire following step were previously swallowed.

This is a regression guard: it confirms the post-spawn parent content (the
parent step's completion breadcrumb and the following step) is emitted live now
that a never-closing subagent fold can no longer wedge the cursor (the §11 fix
closes an unmatched fold on an id mismatch). It would fail again if that fix
regressed.
"""
from __future__ import annotations

import io
import json

from imp.cli import narrative as nv
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.render_text import render_tree
from tests.conftest import FIXTURES

_FIXTURE = FIXTURES / "cc_subagent_stall.jsonl"


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def _events() -> list[dict]:
    return [json.loads(ln) for ln in _FIXTURE.read_text().splitlines() if ln.strip()]


def _live_body(events) -> str:
    out = _Pipe()
    drv = LiveDriver(stream=out, color=False, width=80, status=LiveStatus(out))
    for event in events:
        drv.on_event(event)
    return out.getvalue()


def _log_body(events) -> str:
    tree = nv.build_tree(events)
    return "\n".join(render_tree(tree, full=False, color=False, width=80))


def test_live_emits_content_after_a_subagent_spawn() -> None:
    events = _events()
    live = _live_body(events)
    log = _log_body(events)

    # Sanity: imp log shows the post-spawn parent content (the run did progress).
    assert "task-lead · complete" in log
    assert "commit-check" in log

    # The bug: live emission stalls on the never-closing subagent fold, so the
    # parent step's completion and the entire following step never appear live.
    assert "task-lead · complete" in live, (
        "task-lead completion breadcrumb missing from live output — the live "
        "cursor is wedged behind a never-closing SubagentFold"
    )
    assert "commit-check" in live, (
        "the entire commit-check step after the subagent spawn is missing from "
        "live output — live cursor stalled"
    )
