from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from imp.cli.render_summary import build_summary, overlay_spans

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _ev(eid: str, etype: str, at: int, agent_id: str | None = None,
        **payload: Any) -> dict[str, Any]:
    return {"id": eid, "type": etype, "agent_id": agent_id,
            "timestamp": (_BASE + timedelta(seconds=at)).isoformat(), "payload": payload}


def _dispatch(eid: str, block: str, name: str, at: int) -> dict[str, Any]:
    return _ev(eid, "tool_call", at, "lead", id=block, id_ns="block", name="sub_agent",
               input={"subagent_name": name, "task_description": "go"})


def _result(eid: str, block: str, at: int, *, error: bool) -> dict[str, Any]:
    return _ev(eid, "tool_result", at, "lead", id=block, id_ns="block", content="x",
               error=error)


# `--range 1-3` keeps build's start and both dispatches; every end lies beyond it.
# build has no step_completed: its end is inferred at review's start (80s), less a 30s pause
# = ~50s; errored at 60s.
# explorer: 1s -> 76s = 1m 15s. scout: 2s -> 50s = 48s, failed.
_EVENTS = [
    _ev("st-build", "step_started", 0, step="build", phase="code"),
    _dispatch("tk1", "blk1", "explorer", 1),
    _dispatch("tk2", "blk2", "scout", 2),
    _ev("wp", "workflow_paused", 10, reason="ctrl_c"),
    _ev("wr", "workflow_resumed", 40),
    _result("r2", "blk2", 50, error=True),
    _ev("err", "error", 60, category="provider", kind="crash", message="boom"),
    _result("r1", "blk1", 76, error=False),
    _ev("st-review", "step_started", 80, step="review", phase="code"),
    _ev("sc-review", "step_completed", 90, step="review", action={"verb": "continue"}),
]


def test_overlay_spans_adopts_whole_log_end_of_span() -> None:
    rows = build_summary(_EVENTS[:3])
    overlay_spans(rows, build_summary(_EVENTS))
    [build] = rows
    assert (build.dur_cell("open"), build.errored) == ("~50s", True)
    assert [(sub.name, sub.dur_cell("open"), sub.errored) for sub in build.subagents] == [
        ("explorer", "1m 15s", False), ("scout", "48s", True)]


def test_overlay_spans_adopts_inferred_subagent_end() -> None:
    # Without its result, explorer's end is inferred at review's start: 1s -> 80s.
    full = [e for e in _EVENTS if e["id"] != "r1"]
    rows = build_summary(full[:3])
    overlay_spans(rows, build_summary(full))
    assert rows[0].subagents[0].dur_cell("open") == "~1m 19s"


# A slice starting on build's resume re-entry has a later start, so no full-build step row
# shares its key; its subagent still matches. explorer: 21s -> 30s = 9s.
_REENTRY = [
    _ev("st-build", "step_started", 0, step="build", phase="code"),
    _ev("wp", "workflow_paused", 10, reason="ctrl_c"),
    _ev("wr", "workflow_resumed", 20),
    _ev("st-build-again", "step_started", 20, step="build", phase="code"),
    _dispatch("tk3", "blk3", "explorer", 21),
    _result("r3", "blk3", 30, error=False),
    _ev("sc-build", "step_completed", 40, step="build", action={"verb": "continue"}),
]


def test_overlay_spans_leaves_row_without_full_counterpart_as_sliced() -> None:
    rows = build_summary(_REENTRY[3:5])
    overlay_spans(rows, build_summary(_REENTRY))
    [build] = rows
    assert build.end_ts is None
    assert [(sub.name, sub.dur_cell("open")) for sub in build.subagents] == [("explorer", "9s")]
