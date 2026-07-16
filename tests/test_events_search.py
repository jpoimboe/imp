from __future__ import annotations

import argparse
from typing import Any

import imp.cli.main as m


def _ev(eid: str | None, etype: str, agent_id: str | None = None,
        **payload: Any) -> dict[str, Any]:
    return {"id": eid, "parent_id": None, "agent_id": agent_id,
            "type": etype, "payload": payload}


def _search_ns(query: str, **over: object) -> argparse.Namespace:
    base: dict[str, object] = dict(
        query=query, active=False, backlog=False, archived=False,
        type=None, agent=None, limit=None, list=False, json=False, no_pager=True)
    base.update(over)
    return argparse.Namespace(**base)


def test_selected_events_search_defaults_to_all_three() -> None:
    assert m._selected_events_search(_search_ns("q")) == (True, True, True)


def test_selected_events_search_honours_explicit_flags() -> None:
    assert m._selected_events_search(_search_ns("q", archived=True)) == (False, False, True)
    assert m._selected_events_search(
        _search_ns("q", active=True, backlog=True)) == (True, True, False)


def test_matched_events_returns_only_query_matches() -> None:
    events = [
        _ev("e0", "tool_call", "step:impl/coder", name="bash",
            input={"cmd": "GREPTARGET"}),
        _ev("e1", "tool_result", "step:impl/coder", id="t1", content="unrelated"),
    ]
    matched, handle_map = m._matched_events(events, "greptarget", types=None, agents=None)
    assert [o for o, _ in matched] == [1]
    assert isinstance(handle_map, dict)


def test_matched_events_applies_type_filter() -> None:
    events = [
        _ev("e0", "tool_call", "step:impl/coder", name="bash",
            input={"cmd": "GREPTARGET"}),
        _ev("e1", "tool_result", "step:impl/coder", id="t1", content="GREPTARGET too"),
    ]
    matched, _ = m._matched_events(
        events, "greptarget", types={"tool_result"}, agents=None)
    assert [o for o, _ in matched] == [2]


def test_matched_events_applies_agent_filter() -> None:
    events = [
        _ev("e0", "tool_call", "step:impl/coder", name="bash",
            input={"cmd": "GREPTARGET"}),
        _ev("e1", "tool_call", "step:review/reviewer", name="bash",
            input={"cmd": "GREPTARGET"}),
    ]
    matched, _ = m._matched_events(
        events, "greptarget", types=None, agents={"coder"})
    assert [o for o, _ in matched] == [1]


def test_matched_events_empty_when_no_match() -> None:
    events = [_ev("e0", "tool_call", "step:impl/coder", name="bash", input={"cmd": "x"})]
    matched, _ = m._matched_events(events, "nomatch-zzz", types=None, agents=None)
    assert matched == []
