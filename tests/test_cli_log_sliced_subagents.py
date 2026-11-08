from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from imp.cli import narrative as nv
from imp.cli.render_text import render_tree

_BASE = datetime(2026, 1, 1, tzinfo=UTC)
_PARENT = "step:code/build"
_SUCCESS: dict[str, Any] = {"content": '{"status":"Success"}', "error": False}


def _ev(eid: str, etype: str, agent_id: str | None, s: int,
        **payload: Any) -> dict[str, Any]:
    return {"id": eid, "type": etype, "agent_id": agent_id,
            "timestamp": (_BASE + timedelta(seconds=s)).isoformat(), "payload": payload}


def _start(s: int, step: str = "build") -> dict[str, Any]:
    return _ev(f"st-{step}", "step_started", None, s, step=step, kind="agent", phase="code")


def _done(s: int, step: str = "build") -> dict[str, Any]:
    return _ev(f"sc-{step}", "step_completed", None, s, step=step, kind="agent",
               action={"verb": "continue"}, var_mutations={})


def _dispatch(eid: str, block: str, agent_id: str, name: str, s: int) -> dict[str, Any]:
    return _ev(eid, "tool_call", agent_id, s, id=block, id_ns="block", name="sub_agent",
               input={"subagent_name": name, "task_description": "go"})


def _turn(eid: str, agent_id: str, s: int) -> dict[str, Any]:
    return _ev(eid, "agent_message", agent_id, s, provider="anthropic", model="sub-m",
               raw={"content": [{"type": "text", "text": "working"}]},
               usage={"input_tokens": 1, "output_tokens": 1}, stop_reason="end_turn")


def _result(eid: str, block: str, agent_id: str, s: int, **payload: Any) -> dict[str, Any]:
    return _ev(eid, "tool_result", agent_id, s, id=block, id_ns="block", **payload)


def _closed_log(**close: Any) -> list[dict[str, Any]]:
    """explorer dispatched at 1s and closed by `close` at 76s: a 1m 15s run."""
    return [_start(0), _dispatch("tk1", "blk1", _PARENT, "explorer", 1),
            _turn("m1", "sub:tk1", 2), _result("r1", "blk1", _PARENT, 76, **close),
            _done(80)]


def _nested_log() -> list[dict[str, Any]]:
    """scout (a 30s run) dispatched from inside explorer (a 1m 15s run)."""
    return [_start(0), _dispatch("tk1", "blk1", _PARENT, "explorer", 1),
            _dispatch("tk2", "blk2", "sub:tk1", "scout", 2), _turn("m2", "sub:tk2", 3),
            _result("r2", "blk2", "sub:tk1", 32, **_SUCCESS),
            _result("r1", "blk1", _PARENT, 76, **_SUCCESS), _done(80)]


def _running_log() -> list[dict[str, Any]]:
    return [_start(0), _dispatch("tk1", "blk1", _PARENT, "explorer", 1),
            _turn("m1", "sub:tk1", 2), _turn("m2", "sub:tk1", 3)]


def _backoff_log() -> list[dict[str, Any]]:
    """explorer runs 75s, 30s of it (10s-40s) waiting out a rate-limit backoff."""
    return [_start(0), _dispatch("tk1", "blk1", _PARENT, "explorer", 1),
            _turn("m1", "sub:tk1", 2),
            _ev("rt", "agent_retry", "sub:tk1", 10, category="rate_limit", attempt=1,
                backoff_seconds=30.0),
            _ev("be", "backoff_ended", "sub:tk1", 40, reason="retry_now"),
            _result("r1", "blk1", _PARENT, 76, **_SUCCESS), _done(80)]


def _rows(lines: Iterable[str], name: str) -> list[str]:
    return [line for line in lines if line.lstrip().startswith(name)]


def _overlaid_rows(events: list[dict[str, Any]], keep: slice, name: str) -> list[str]:
    tree = nv.build_tree(events[keep])
    nv.overlay_subagent_state(tree, nv.build_tree(events))
    return _rows(render_tree(tree, full=False, color=False, width=200), name)


def test_overlay_reaches_nested_fold_whose_outer_dispatch_precedes_slice() -> None:
    # Sliced, scout is a top-level fold; in the full tree it is nested in explorer.
    assert _overlaid_rows(_nested_log(), slice(2, 4), "scout") == ["  scout · sub-m · 30s"]


def test_overlay_adopts_backoff_wait_straddling_slice_end() -> None:
    # The slice holds the agent_retry but not the backoff_ended: 75s - 30s backoff.
    assert _overlaid_rows(_backoff_log(), slice(0, 4), "explorer") == [
        "  explorer · sub-m · 45s"]


def test_overlay_keeps_genuinely_running_fold_running() -> None:
    assert _overlaid_rows(_running_log(), slice(0, 3), "explorer") == [
        "  explorer · sub-m · running"]


def test_overlay_leaves_fold_without_full_counterpart_as_sliced() -> None:
    tree = nv.build_tree(_closed_log(**_SUCCESS)[:3])
    nv.overlay_subagent_state(tree, nv.build_tree([]))
    assert _rows(render_tree(tree, full=False, color=False, width=200), "explorer") == [
        "  explorer · sub-m · running"]
