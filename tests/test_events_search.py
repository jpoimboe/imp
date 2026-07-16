from __future__ import annotations

import argparse
import json as _json
from pathlib import Path
from typing import Any

import imp.cli.main as m
from imp.engine import archive


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


class _FakeStore:
    def __init__(self, events_by_id: dict[str, list[dict[str, Any]]]) -> None:
        self._events = events_by_id

    def scan_homes(self) -> list[object]:
        return []

    def get_events(self, quest_id: str) -> list[dict[str, Any]] | None:
        return self._events.get(quest_id)


def _row(cid: str, title: str, last: str = "2026-06-11T00:00:00+00:00",
         ) -> tuple[str, str, str, str, str, str, str | None, str | None]:
    return (cid, "planning", "spec", last, title,
            "2026-06-01T00:00:00+00:00", None, None)


def test_events_search_active_includes_only_quests_with_matching_events(
        monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("IMP_SESSION_SOCKET", raising=False)
    monkeypatch.setattr(m, "_status_rows", lambda control, scanned=None: [_row("aaa111", "Alpha"),
                                                                      _row("bbb222", "Beta")])
    store = _FakeStore({
        "aaa111": [_ev("e0", "tool_call", "step:impl/coder", name="bash",
                       input={"cmd": "GREPTARGET"})],
        "bbb222": [_ev("e0", "tool_call", "step:impl/coder", name="bash",
                       input={"cmd": "unrelated"})]})
    monkeypatch.setattr(m, "make_store", lambda control: store)
    hits = m._events_search_active(tmp_path, "greptarget", types=None, agents=None)
    assert [h.quest_id for h in hits] == ["aaa111"]
    assert hits[0].zone == "active"
    assert hits[0].match_count == 1
    assert [o for o, _ in hits[0].events] == [1]


def test_events_search_backlog_matches_title_and_intent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("IMP_SESSION_SOCKET", raising=False)
    monkeypatch.setattr(m, "_backlog_rows", lambda control: [_row("ccc333", "Payments")])
    monkeypatch.setattr(m, "_backlog_seed_opening",
                        lambda control, qid: "add a GREPTARGET to checkout")
    hits = m._events_search_backlog(tmp_path, "greptarget", types=None, agents=None)
    assert [h.quest_id for h in hits] == ["ccc333"]
    assert hits[0].zone == "backlog"
    assert hits[0].events == []
    assert hits[0].match_count == 1


def test_events_search_backlog_matches_on_title(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("IMP_SESSION_SOCKET", raising=False)
    monkeypatch.setattr(
        m, "_backlog_rows", lambda control: [_row("ccc333", "GREPTARGET dashboard")])
    monkeypatch.setattr(m, "_backlog_seed_opening", lambda control, qid: "unrelated intent")
    hits = m._events_search_backlog(tmp_path, "greptarget", types=None, agents=None)
    assert [h.quest_id for h in hits] == ["ccc333"]


def test_events_search_backlog_matches_on_quest_id(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("IMP_SESSION_SOCKET", raising=False)
    monkeypatch.setattr(m, "_backlog_rows", lambda control: [_row("greptarget99", "Some title")])
    monkeypatch.setattr(m, "_backlog_seed_opening", lambda control, qid: "unrelated intent")
    hits = m._events_search_backlog(tmp_path, "greptarget", types=None, agents=None)
    assert [h.quest_id for h in hits] == ["greptarget99"]


def test_events_search_backlog_skipped_when_type_filter_present(
        monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("IMP_SESSION_SOCKET", raising=False)
    monkeypatch.setattr(m, "_backlog_rows", lambda control: [_row("ccc333", "GREPTARGET")])
    monkeypatch.setattr(m, "_backlog_seed_opening", lambda control, qid: "GREPTARGET")
    assert m._events_search_backlog(
        tmp_path, "greptarget", types={"tool_call"}, agents=None) == []


def _archive_events(tmp_path: Path, quest_id: str, title: str,
                    events_text: str, status: str = "completed") -> None:
    events = tmp_path / f"{quest_id}.jsonl"
    events.write_text(events_text)
    (tmp_path / f"{quest_id}.quest.json").write_text(_json.dumps({
        "id": quest_id, "title": title, "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    (tmp_path / f"{quest_id}.state.json").write_text(_json.dumps({
        "id": quest_id, "status": status, "step": "finalize"}))
    archive.archive_quest(
        tmp_path, quest_id, events_path=events,
        quest_json_path=tmp_path / f"{quest_id}.quest.json",
        state_json_path=tmp_path / f"{quest_id}.state.json")


def test_events_search_archived_end_to_end(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("IMP_SESSION_SOCKET", raising=False)
    line = _json.dumps({
        "id": "e0", "parent_id": None, "agent_id": "step:impl/coder",
        "timestamp": "2026-06-01T00:00:01+00:00", "type": "tool_call",
        "payload": {"name": "bash", "input": {"cmd": "GREPTARGET"}}}) + "\n"
    _archive_events(tmp_path, "arch01", "Archived one", line)
    _archive_events(tmp_path, "arch02", "Archived two",
                    _json.dumps({"id": "e0", "type": "tool_call",
                                 "payload": {"name": "bash",
                                             "input": {"cmd": "other"}}}) + "\n")
    hits = m._events_search_archived(tmp_path, "greptarget", types=None, agents=None)
    assert [h.quest_id for h in hits] == ["arch01"]
    assert hits[0].zone == "archive"
    assert [o for o, _ in hits[0].events] == [1]
