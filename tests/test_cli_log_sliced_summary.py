from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import imp.cli.main as m
import imp.cli.render_summary as rs
import imp.cli.store as s
from imp.cli import palette as pal
from imp.cli.render_summary import build_summary, overlay_spans

_BASE = datetime(2026, 1, 1, tzinfo=UTC)
_CID = "slcsum"
_ERROR_SGR = pal.ansi("x", *pal.NODE_COLORS["error"], enabled=True).split("x")[0]


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
    assert [(sub.name, sub.dur_cell("open"), sub.stop) for sub in build.subagents] == [
        ("explorer", "1m 15s", None), ("scout", "48s", "failed")]


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


def _summary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: str, *argv: str,
             color: bool = False) -> str:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_resolve_id", lambda control, token: _CID)
    monkeypatch.setattr(s, "_load_events_for_id", lambda control, c: _EVENTS)
    monkeypatch.setattr(
        s, "_row_for_id",
        lambda control, c: (_CID, status, "code", "2026-01-01T00:01:30+00:00",
                            _CID, "2026-01-01T00:00:00+00:00", None, None, None))
    if color:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
    else:
        monkeypatch.setenv("NO_COLOR", "1")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = m.main(["log", _CID, "-s", *argv])
    assert rc == 0, err.getvalue()
    return out.getvalue()


def _row(out: str, name: str) -> str:
    """The row for `name`, its column padding collapsed so a narrower view compares equal."""
    rows = [" ".join(ln.split()) for ln in out.splitlines()
            if name in ln and not ln.lstrip().startswith(("#", "**"))]
    assert len(rows) == 1, rows
    return rows[0]


_STATUSES = pytest.mark.parametrize("status", ["paused", "completed"])


@_STATUSES
@pytest.mark.parametrize(("name", "whole_row"), [
    pytest.param("build", "build ~50s — — —", id="step-inferred-end"),
    pytest.param("explorer", "↳ explorer 1m 15s — — —", id="subagent"),
])
def test_range_summary_row_matches_whole_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, name: str,
    whole_row: str,
) -> None:
    whole = _row(_summary(monkeypatch, tmp_path, status), name)
    sliced = _row(_summary(monkeypatch, tmp_path, status, "--range", "1-3"), name)
    assert whole == whole_row
    assert sliced == whole


@_STATUSES
def test_range_summary_marks_cut_off_failed_subagent_errored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    # errored shows only as color, so compare the colored rows.
    whole = _row(_summary(monkeypatch, tmp_path, status, color=True), "scout")
    sliced = _row(_summary(monkeypatch, tmp_path, status, "--range", "1-3", color=True),
                  "scout")
    assert _ERROR_SGR in whole
    assert sliced == whole


@_STATUSES
@pytest.mark.parametrize(("name", "whole_row"), [
    pytest.param("build", "| build | ~50s | — | — | — | — | — |", id="step-inferred-end"),
    pytest.param("explorer", "| ↳ explorer | 1m 15s | — | — | — | — | — |", id="subagent"),
])
def test_range_summary_md_row_matches_whole_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, name: str,
    whole_row: str,
) -> None:
    whole = _row(_summary(monkeypatch, tmp_path, status, "--md", "-"), name)
    sliced = _row(_summary(monkeypatch, tmp_path, status, "--range", "1-3", "--md", "-"),
                  name)
    assert whole == whole_row
    assert sliced == whole


def test_unfiltered_summary_builds_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = rs.build_summary

    def counting(events: Any, *a: Any, **kw: Any) -> list[Any]:
        calls.append(1)
        return real(events, *a, **kw)

    monkeypatch.setattr(rs, "build_summary", counting)
    m._log_summary(_EVENTS, _EVENTS)
    assert len(calls) == 1
    m._log_summary(_EVENTS[:3], _EVENTS)
    assert len(calls) == 3
