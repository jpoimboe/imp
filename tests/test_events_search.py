from __future__ import annotations

import argparse
import io
import json as _json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest

import imp.cli.main as m
import imp.cli.store as s
from imp.cli.main import main
from imp.cli.store import HomeDigest
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
         ) -> tuple[str, str, str, str, str, str, str | None, str | None, str | None]:
    return (cid, "planning", "spec", last, title,
            "2026-06-01T00:00:00+00:00", None, None, None)


def _digest(quest_id: str, seed: str) -> HomeDigest:
    """A live backlog home as scan_homes reports it; the seed body lives in meta."""
    return HomeDigest(
        quest_id=quest_id, quest_json=b"{}", state_json=None,
        meta={"id": quest_id, "materialized": False, "origin": "backlog",
              "seed_opening": seed},
        last_iso="2026-06-11T00:00:00+00:00", has_user_message=False,
        prose="", intent_title=None, lock_live=False)


class _BacklogStore:
    def __init__(self, homes: list[HomeDigest]) -> None:
        self._homes = homes

    def scan_homes(self) -> list[HomeDigest]:
        return self._homes


def test_events_search_active_includes_only_quests_with_matching_events(
        monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(s, "_status_rows", lambda control, scanned=None: [_row("aaa111", "Alpha"),
                                                                      _row("bbb222", "Beta")])
    store = _FakeStore({
        "aaa111": [_ev("e0", "tool_call", "step:impl/coder", name="bash",
                       input={"cmd": "GREPTARGET"})],
        "bbb222": [_ev("e0", "tool_call", "step:impl/coder", name="bash",
                       input={"cmd": "unrelated"})]})
    monkeypatch.setattr("imp.cli.store.make_store", lambda control: store)
    hits = m._events_search_active(tmp_path, "greptarget", types=None, agents=None)
    assert [h.quest_id for h in hits] == ["aaa111"]
    assert hits[0].zone == "active"
    assert hits[0].match_count == 1
    assert [o for o, _ in hits[0].events] == [1]


def test_events_search_backlog_matches_title_and_intent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        s, "_backlog_rows", lambda control, scanned=None: [_row("ccc333", "Payments")])
    monkeypatch.setattr(
        "imp.cli.store.make_store",
        lambda control: _BacklogStore([_digest("ccc333", "add a GREPTARGET to checkout")]))
    hits = m._events_search_backlog(tmp_path, "greptarget", types=None, agents=None)
    assert [h.quest_id for h in hits] == ["ccc333"]
    assert hits[0].zone == "backlog"
    assert hits[0].events == []
    assert hits[0].match_count == 1


def test_events_search_backlog_matches_on_title(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        s, "_backlog_rows",
        lambda control, scanned=None: [_row("ccc333", "GREPTARGET dashboard")])
    monkeypatch.setattr(
        "imp.cli.store.make_store",
        lambda control: _BacklogStore([_digest("ccc333", "unrelated intent")]))
    hits = m._events_search_backlog(tmp_path, "greptarget", types=None, agents=None)
    assert [h.quest_id for h in hits] == ["ccc333"]


def test_events_search_backlog_matches_on_quest_id(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        s, "_backlog_rows",
        lambda control, scanned=None: [_row("greptarget99", "Some title")])
    monkeypatch.setattr(
        "imp.cli.store.make_store",
        lambda control: _BacklogStore([_digest("greptarget99", "unrelated intent")]))
    hits = m._events_search_backlog(tmp_path, "greptarget", types=None, agents=None)
    assert [h.quest_id for h in hits] == ["greptarget99"]


def test_events_search_backlog_skipped_when_type_filter_present(
        monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        s, "_backlog_rows", lambda control, scanned=None: [_row("ccc333", "GREPTARGET")])
    monkeypatch.setattr(
        "imp.cli.store.make_store",
        lambda control: _BacklogStore([_digest("ccc333", "GREPTARGET")]))
    assert m._events_search_backlog(
        tmp_path, "greptarget", types={"tool_call"}, agents=None) == []


def _archive_events(tmp_path: Path, quest_id: str, title: str,
                    events_text: str, status: str = "merged") -> None:
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


def test_events_search_archived_routes_through_store(monkeypatch, tmp_path: Path) -> None:
    class _FakeStore:
        def search_event_candidates(self, query):
            return [("arch01", "Archived one", "merged", "2026-06-11T00:00:00+00:00")]

        def get_events(self, cid):
            return [_ev("e0", "tool_call", name="bash", input={"cmd": "GREPTARGET"})]

    monkeypatch.setattr("imp.cli.store.make_store", lambda control: _FakeStore())

    def _boom(*a, **k):
        raise AssertionError("client path must not open the archive index directly")

    monkeypatch.setattr("imp.engine.archive.read.search_archive_events_content", _boom)

    hits = m._events_search_archived(tmp_path, "greptarget", types=None, agents=None)
    assert [h.quest_id for h in hits] == ["arch01"]
    assert hits[0].zone == "archive"
    assert hits[0].last_activity == "2026-06-11T00:00:00+00:00"


def _hit(cid: str, title: str, zone: str, events: list[tuple[int, dict[str, Any]]],
         match_count: int, last: str = "2026-06-11T00:00:00+00:00") -> m.EventSearchHit:
    return m.EventSearchHit(cid, title, zone, last, events, {}, match_count)


def _bounded_hits() -> list[m.EventSearchHit]:
    def events(cid: str) -> list[tuple[int, dict[str, Any]]]:
        return [
            (ordinal, _ev(f"{cid}-{ordinal}", "tool_call", "step:impl/coder",
                          name="bash",
                          input={"cmd": f"GREPTARGET {cid} event {ordinal}"}))
            for ordinal in range(1, 6)
        ]

    return [
        _hit("arc_old", "Archive Old", "archive", events("arc_old"), 5,
             "2026-06-10T00:00:00+00:00"),
        _hit("act_old", "Active Old", "active", events("act_old"), 5,
             "2026-06-11T00:00:00+00:00"),
        _hit("arc_new", "Archive New", "archive", events("arc_new"), 5,
             "2026-06-15T00:00:00+00:00"),
        _hit("act_new", "Active New", "active", events("act_new"), 5,
             "2026-06-16T00:00:00+00:00"),
        _hit("bkl001", "Backlog One", "backlog", [], 1,
             "2026-06-12T00:00:00+00:00"),
    ]


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


def test_cmd_events_search_default_rich_is_bounded(monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setattr(m, "_events_search", lambda *a, **k: _bounded_hits())

    rc = m._cmd_events_search(_search_ns("GREPTARGET"), tmp_path)

    out = capsys.readouterr().out
    assert rc == 0
    assert '21 matches in 5 quests for "GREPTARGET"' in out
    assert out.index("act_new · Active New · active") < out.index(
        "act_old · Active Old · active") < out.index("bkl001 · Backlog One · backlog")
    assert "arc_new" not in out and "arc_old" not in out
    assert "#4" not in out
    assert out.count("  … +2 more") == 2
    assert "… +2 more quests" in out


def test_cmd_events_search_human_presentation_and_spacing(
        monkeypatch, tmp_path: Path) -> None:
    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(m, "_events_search", lambda *a, **k: _bounded_hits())
    monkeypatch.setattr(m, "should_use_color", lambda stream: True)
    out = _Tty()
    monkeypatch.setattr(m.sys, "stdout", out)

    assert m._cmd_events_search(_search_ns("GREPTARGET"), tmp_path) == 0
    rich = out.getvalue()
    assert "\x1b[2m21 matches in 5 quests for \"GREPTARGET\"\x1b[0m" in rich
    assert "\x1b[33mact_new\x1b[0m" in rich
    assert "\x1b[1m\x1b[33mGREPTARGET\x1b[0m" in rich
    assert "\x1b[2m  … +2 more\x1b[0m" in rich
    assert rich.endswith("\n\n") and not rich.endswith("\n\n\n")

    out.seek(0)
    out.truncate(0)
    assert m._cmd_events_search(
        _search_ns("GREPTARGET", list=True), tmp_path) == 0
    listed = out.getvalue()
    assert listed.endswith("\n\n") and not listed.endswith("\n\n\n")


def test_cmd_events_search_per_quest_event_overflow(monkeypatch, capsys, tmp_path: Path) -> None:
    events = [(i, _ev(f"e{i}", "tool_call", None, name="bash",
                      input={"cmd": "GREPTARGET"})) for i in range(1, 6)]
    monkeypatch.setattr(m, "_events_search",
                        lambda *a, **k: [_hit("aaa111", "Alpha", "active", events, 5)])
    rc = m._cmd_events_search(_search_ns("GREPTARGET"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "  … +2 more" in out


def test_cmd_events_search_default_json_is_bounded_and_byte_clean(
        monkeypatch, capsys, tmp_path: Path) -> None:
    source_ordinals = [7, 11, 19, 23, 29]
    hits = _bounded_hits()
    hits[3] = _hit(
        "act_new", "Active New", "active",
        [(ordinal, _ev(f"act_new-{ordinal}", "tool_call", "step:impl/coder",
                       name="bash",
                       input={"cmd": f"GREPTARGET act_new event {ordinal}"}))
         for ordinal in source_ordinals],
        5, "2026-06-16T00:00:00+00:00")
    monkeypatch.setattr(m, "_events_search", lambda *a, **k: hits)

    assert m._cmd_events_search(_search_ns("GREPTARGET", json=True), tmp_path) == 0

    out = capsys.readouterr().out
    payload = _json.loads(out)
    assert isinstance(payload, list)
    assert [hit["quest_id"] for hit in payload] == ["act_new", "act_old", "bkl001"]
    assert set(payload[0]) == {"quest_id", "title", "zone", "last_activity", "events"}
    assert [event["ordinal"] for event in payload[0]["events"]] == [7, 11, 19]
    assert payload[0]["events"][0]["payload"]["input"]["cmd"] == (
        "GREPTARGET act_new event 7")
    assert payload[2]["events"] == []
    assert "truncated" not in out and "\x1b[" not in out


def test_main_events_search_json_wins_over_list_and_is_bounded(
        monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_events_search", lambda *a, **k: _bounded_hits())
    monkeypatch.setattr(m, "_apply_theme", lambda: None)

    assert main(["events", "search", "GREPTARGET", "--list", "--json", "--no-pager"]) == 0

    assert [hit["quest_id"] for hit in _json.loads(capsys.readouterr().out)] == [
        "act_new", "act_old", "bkl001"]


def test_cmd_events_search_list_is_exhaustive_but_limit_bounds_all_modes(
        monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setattr(m, "_events_search", lambda *a, **k: _bounded_hits())

    assert m._cmd_events_search(_search_ns("GREPTARGET", list=True), tmp_path) == 0
    listed = capsys.readouterr().out
    assert [cid for cid in ("act_new", "act_old", "bkl001", "arc_new", "arc_old")
            if cid in listed] == ["act_new", "act_old", "bkl001", "arc_new", "arc_old"]

    assert m._cmd_events_search(_search_ns("GREPTARGET", limit=2), tmp_path) == 0
    assert "… +3 more quests" in capsys.readouterr().out

    assert m._cmd_events_search(
        _search_ns("GREPTARGET", list=True, limit=2), tmp_path) == 0
    assert [line.split(" · ")[0] for line in capsys.readouterr().out.splitlines()] == [
        "act_new", "act_old"]

    assert m._cmd_events_search(
        _search_ns("GREPTARGET", json=True, limit=2), tmp_path) == 0
    payload = _json.loads(capsys.readouterr().out)
    assert [hit["quest_id"] for hit in payload] == ["act_new", "act_old"]
    assert [event["ordinal"] for event in payload[0]["events"]] == [1, 2, 3]


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


def test_cmd_events_search_filters_before_sorting_and_bounding(
        monkeypatch, capsys, tmp_path: Path) -> None:
    rows = [
        _row("drop_new", "Drop New", "2026-06-20T00:00:00+00:00"),
        _row("keep_old", "Keep Old", "2026-06-10T00:00:00+00:00"),
        _row("keep_mid", "Keep Mid", "2026-06-11T00:00:00+00:00"),
        _row("keep_new", "Keep New", "2026-06-12T00:00:00+00:00"),
    ]
    events = {
        "drop_new": [_ev("e1", "tool_call", "step:review/reviewer", name="bash",
                          input={"cmd": "GREPTARGET"})],
        "keep_old": [_ev("e1", "tool_result", "step:review/reviewer",
                          content="GREPTARGET")],
        "keep_mid": [_ev("e1", "tool_result", "step:review/reviewer",
                          content="GREPTARGET")],
        "keep_new": [_ev("e1", "tool_result", "step:review/reviewer",
                          content="GREPTARGET")],
    }
    monkeypatch.setattr(s, "_status_rows", lambda control, scanned=None: rows)
    monkeypatch.setattr("imp.cli.store.make_store", lambda control: _FakeStore(events))

    assert m._cmd_events_search(
        _search_ns("GREPTARGET", active=True, json=True,
                   type=["tool_result"], agent=["reviewer"]), tmp_path) == 0

    assert [hit["quest_id"] for hit in _json.loads(capsys.readouterr().out)] == [
        "keep_new", "keep_mid", "keep_old"]


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
