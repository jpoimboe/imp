from __future__ import annotations

import argparse
import io
import json as _json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest

import imp.cli.main as m
from imp.cli.main import main
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


def test_matched_events_resolves_handle_from_filtered_out_dispatch() -> None:
    # The subagent-dispatch tool_call (id D1) defines the handle coder-1 for
    # sub:D1. A --type tool_result filter drops that dispatch but keeps the
    # subagent's own tool_result; the handle must still resolve because
    # subagent_handle_map is computed over the FULL event list, before the
    # filter. Computing it post-filter would lose the dispatch and the handle.
    events = [
        _ev(None, "step_started", step="task-lead"),
        _ev("D1", "tool_call", "step:main/task-lead",
            name="sub_agent", input={"subagent_name": "coder"}),
        _ev("e1", "tool_result", "sub:D1", id="t1", content="GREPTARGET body"),
        _ev(None, "step_completed", step="task-lead"),
    ]
    matched, handle_map = m._matched_events(
        events, "greptarget", types={"tool_result"}, agents=None)
    assert [o for o, _ in matched] == [3]
    assert handle_map["sub:D1"] == "coder-1"


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
    monkeypatch.setattr(m, "_backlog_rows", lambda control: [_row("ccc333", "Payments")])
    monkeypatch.setattr(m, "_backlog_seed_opening",
                        lambda control, qid: "add a GREPTARGET to checkout")
    hits = m._events_search_backlog(tmp_path, "greptarget", types=None, agents=None)
    assert [h.quest_id for h in hits] == ["ccc333"]
    assert hits[0].zone == "backlog"
    assert hits[0].events == []
    assert hits[0].match_count == 1


def test_events_search_backlog_matches_on_title(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        m, "_backlog_rows", lambda control: [_row("ccc333", "GREPTARGET dashboard")])
    monkeypatch.setattr(m, "_backlog_seed_opening", lambda control, qid: "unrelated intent")
    hits = m._events_search_backlog(tmp_path, "greptarget", types=None, agents=None)
    assert [h.quest_id for h in hits] == ["ccc333"]


def test_events_search_backlog_matches_on_quest_id(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(m, "_backlog_rows", lambda control: [_row("greptarget99", "Some title")])
    monkeypatch.setattr(m, "_backlog_seed_opening", lambda control, qid: "unrelated intent")
    hits = m._events_search_backlog(tmp_path, "greptarget", types=None, agents=None)
    assert [h.quest_id for h in hits] == ["greptarget99"]


def test_events_search_backlog_skipped_when_type_filter_present(
        monkeypatch, tmp_path: Path) -> None:
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


def _hit(cid: str, title: str, zone: str, events: list[tuple[int, dict[str, Any]]],
         match_count: int, last: str = "2026-06-11T00:00:00+00:00") -> m.EventSearchHit:
    return m.EventSearchHit(cid, title, zone, last, events, {}, match_count)


def test_cmd_events_search_rich_header_and_digest(monkeypatch, capsys, tmp_path: Path) -> None:
    hit = _hit("aaa111", "Alpha", "active",
               [(1, _ev("e0", "tool_call", "step:impl/coder", name="bash",
                        input={"cmd": "GREPTARGET"}))], 1)
    monkeypatch.setattr(m, "_events_search", lambda *a, **k: [hit])
    rc = m._cmd_events_search(_search_ns("GREPTARGET"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert '1 match in 1 quest for "GREPTARGET"' in out
    assert "aaa111 · Alpha · active" in out
    assert "#1" in out and "tool_call" in out and "GREPTARGET" in out


def test_cmd_events_search_empty_message(monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setattr(m, "_events_search", lambda *a, **k: [])
    rc = m._cmd_events_search(_search_ns("zzz"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert '0 matches for "zzz"' in out


def test_cmd_events_search_quest_cap_and_overflow(monkeypatch, capsys, tmp_path: Path) -> None:
    hits = [_hit(f"q{i}", f"T{i}", "archive",
                 [(1, _ev("e0", "tool_call", None, name="bash",
                          input={"cmd": "GREPTARGET"}))], 1,
                 last=f"2026-06-1{i}T00:00:00+00:00") for i in range(5)]
    monkeypatch.setattr(m, "_events_search", lambda *a, **k: hits)
    rc = m._cmd_events_search(_search_ns("GREPTARGET"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "… +2 more quests" in out


def test_cmd_events_search_limit_raises_cap(monkeypatch, capsys, tmp_path: Path) -> None:
    hits = [_hit(f"q{i}", f"T{i}", "archive",
                 [(1, _ev("e0", "tool_call", None, name="bash",
                          input={"cmd": "GREPTARGET"}))], 1,
                 last=f"2026-06-1{i}T00:00:00+00:00") for i in range(5)]
    monkeypatch.setattr(m, "_events_search", lambda *a, **k: hits)
    rc = m._cmd_events_search(_search_ns("GREPTARGET", limit=10), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "more quests" not in out


def test_cmd_events_search_per_quest_event_overflow(monkeypatch, capsys, tmp_path: Path) -> None:
    events = [(i, _ev(f"e{i}", "tool_call", None, name="bash",
                      input={"cmd": "GREPTARGET"})) for i in range(1, 6)]
    monkeypatch.setattr(m, "_events_search",
                        lambda *a, **k: [_hit("aaa111", "Alpha", "active", events, 5)])
    rc = m._cmd_events_search(_search_ns("GREPTARGET"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "  … +2 more" in out


def test_cmd_events_search_list_mode(monkeypatch, capsys, tmp_path: Path) -> None:
    hits = [_hit("aaa111", "Alpha", "active", [(1, _ev("e0", "tool_call"))], 1),
            _hit("bbb222", "Beta", "archive", [(1, _ev("e0", "tool_call"))], 1)]
    monkeypatch.setattr(m, "_events_search", lambda *a, **k: hits)
    rc = m._cmd_events_search(_search_ns("q", list=True), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "aaa111 · Alpha · active" in out
    assert "bbb222 · Beta · archive" in out
    assert "match in" not in out


def test_cmd_events_search_json_mode(monkeypatch, capsys, tmp_path: Path) -> None:
    import json as _json
    hit = _hit("aaa111", "Alpha", "active",
               [(7, _ev("e0", "tool_call", "step:impl/coder", name="bash",
                        input={"cmd": "GREPTARGET"}))], 1)
    monkeypatch.setattr(m, "_events_search", lambda *a, **k: [hit])
    rc = m._cmd_events_search(_search_ns("GREPTARGET", json=True), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    payload = _json.loads(out)
    assert payload[0]["quest_id"] == "aaa111"
    assert payload[0]["zone"] == "active"
    assert payload[0]["events"][0]["ordinal"] == 7
    assert payload[0]["events"][0]["type"] == "tool_call"


def test_cmd_events_search_rich_header_plural_nouns(
        monkeypatch, capsys, tmp_path: Path) -> None:
    hits = [
        _hit("q1", "T1", "active",
             [(1, _ev("e0", "tool_call", name="bash", input={"cmd": "GREPTARGET"})),
              (2, _ev("e1", "tool_call", name="bash", input={"cmd": "GREPTARGET"}))],
             match_count=2),
        _hit("q2", "T2", "active",
             [(1, _ev("e0", "tool_call", name="bash", input={"cmd": "GREPTARGET"})),
              (2, _ev("e1", "tool_call", name="bash", input={"cmd": "GREPTARGET"}))],
             match_count=2),
    ]
    monkeypatch.setattr(m, "_events_search", lambda *a, **k: hits)
    rc = m._cmd_events_search(_search_ns("GREPTARGET"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert '4 matches in 2 quests for "GREPTARGET"' in out


def test_cmd_events_search_zone_major_then_last_activity_desc_sort(
        monkeypatch, capsys, tmp_path: Path) -> None:
    act_new = _hit("act_new", "Active New", "active",
                   [(1, _ev("e0", "tool_call", name="bash", input={"cmd": "GREPTARGET"}))],
                   match_count=1, last="2026-06-15T00:00:00+00:00")
    act_old = _hit("act_old", "Active Old", "active",
                   [(1, _ev("e0", "tool_call", name="bash", input={"cmd": "GREPTARGET"}))],
                   match_count=1, last="2026-06-10T00:00:00+00:00")
    bkl = _hit("bkl001", "Backlog One", "backlog",
               [(1, _ev("e0", "tool_call", name="bash", input={"cmd": "GREPTARGET"}))],
               match_count=1, last="2026-06-11T00:00:00+00:00")
    arc = _hit("arc001", "Archive One", "archive",
               [(1, _ev("e0", "tool_call", name="bash", input={"cmd": "GREPTARGET"}))],
               match_count=1, last="2026-06-12T00:00:00+00:00")
    # deliberately scrambled — correct sort must reorder them
    monkeypatch.setattr(m, "_events_search", lambda *a, **k: [arc, act_old, bkl, act_new])
    rc = m._cmd_events_search(_search_ns("GREPTARGET", limit=10), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    # zone-major (active→backlog→archive), then last-activity descending within zone
    idx_act_new = out.index("act_new")
    idx_act_old = out.index("act_old")
    idx_bkl = out.index("bkl001")
    idx_arc = out.index("arc001")
    assert idx_act_new < idx_act_old < idx_bkl < idx_arc


def test_main_routes_events_search_against_archive(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    import json as _json
    line = _json.dumps({
        "id": "e0", "type": "tool_call",
        "payload": {"name": "bash", "input": {"cmd": "GREPTARGET"}}}) + "\n"
    _archive_events(tmp_path, "arch01", "Archived one", line)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["events", "search", "GREPTARGET", "--archived", "--no-pager"])
    out = buf.getvalue()
    assert rc == 0
    assert "arch01" in out
    assert '1 match in 1 quest for "GREPTARGET"' in out


def test_main_events_id_still_flat_after_search_route(monkeypatch, tmp_path: Path) -> None:
    called: dict[str, object] = {}

    def _fake_cmd_events(args: argparse.Namespace) -> int:
        called["token"] = args.token
        return 0

    monkeypatch.setattr(m, "_cmd_events", _fake_cmd_events)
    monkeypatch.setattr(m, "_apply_theme", lambda: None)
    rc = main(["events", "searchxyz"])
    assert rc == 0
    assert called["token"] == "searchxyz"


def test_events_search_help_exits_zero(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["events", "search", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "events search" in out
