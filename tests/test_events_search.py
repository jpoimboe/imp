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
from imp.cli import palette as pal
from imp.cli.main import main
from imp.cli.store import HomeDigest, SearchZones
from imp.engine import archive


def _ev(eid: str | None, etype: str, agent_id: str | None = None,
        **payload: Any) -> dict[str, Any]:
    return {"id": eid, "parent_id": None, "agent_id": agent_id,
            "type": etype, "payload": payload}


def _search_ns(query: str, **over: object) -> argparse.Namespace:
    base: dict[str, object] = dict(
        query=query, active=False, backlog=False, completed=False, abandoned=False,
        archived=False,
        type=None, agent=None, limit=None, all=False, list=False, json=False, no_pager=True)
    base.update(over)
    return argparse.Namespace(**base)


def test_selected_search_on_events_namespace_defaults_to_everything() -> None:
    assert m._selected_search(_search_ns("q"), None) == SearchZones(
        True, True, frozenset({"completed", "abandoned", "archived"}), None)


def test_selected_search_on_events_namespace_honours_explicit_flags() -> None:
    assert m._selected_search(_search_ns("q", archived=True), None) == SearchZones(
        False, False, frozenset({"archived"}), None)
    assert m._selected_search(
        _search_ns("q", active=True, completed=True, abandoned=True), None) == SearchZones(
        True, False, frozenset({"completed", "abandoned"}), None)
    assert m._selected_search(
        _search_ns("q", active=True, backlog=True), None) == SearchZones(
        True, True, frozenset(), None)


def test_matched_events_returns_only_query_matches() -> None:
    events = [
        _ev("e0", "tool_call", "step:impl/coder", name="bash",
            input={"cmd": "GREPTARGET"}),
        _ev("e1", "tool_result", "step:impl/coder", id="t1", content="unrelated"),
    ]
    matched, handle_map, overlays = m._matched_events(events, "greptarget", types=None, agents=None)
    assert [o for o, _ in matched] == [1]
    assert isinstance(handle_map, dict)
    assert isinstance(overlays, dict)


def test_matched_events_applies_type_filter() -> None:
    events = [
        _ev("e0", "tool_call", "step:impl/coder", name="bash",
            input={"cmd": "GREPTARGET"}),
        _ev("e1", "tool_result", "step:impl/coder", id="t1", content="GREPTARGET too"),
    ]
    matched, _, _overlays = m._matched_events(
        events, "greptarget", types={"tool_result"}, agents=None)
    assert [o for o, _ in matched] == [2]


def test_matched_events_applies_agent_filter() -> None:
    events = [
        _ev("e0", "tool_call", "step:impl/coder", name="bash",
            input={"cmd": "GREPTARGET"}),
        _ev("e1", "tool_call", "step:review/reviewer", name="bash",
            input={"cmd": "GREPTARGET"}),
    ]
    matched, _, _overlays = m._matched_events(
        events, "greptarget", types=None, agents={"coder"})
    assert [o for o, _ in matched] == [1]


def test_matched_events_empty_when_no_match() -> None:
    events = [_ev("e0", "tool_call", "step:impl/coder", name="bash", input={"cmd": "x"})]
    matched, _, _overlays = m._matched_events(events, "nomatch-zzz", types=None, agents=None)
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
    matched, handle_map, _overlays = m._matched_events(
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
    assert hits[0].intent is None   # titled


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
    assert hits[0].intent is None   # titled: the seed is not a name source


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
                    events_text: str, status: str = "completed",
                    seed_opening: str | None = None) -> None:
    events = tmp_path / f"{quest_id}.jsonl"
    events.write_text(events_text)
    meta: dict[str, Any] = {
        "id": quest_id, "title": title, "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}
    if seed_opening is not None:
        meta["seed_opening"] = seed_opening
    (tmp_path / f"{quest_id}.quest.json").write_text(_json.dumps(meta))
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
    hits = m._events_search_archived(tmp_path, "greptarget", types=None, agents=None,
                                     statuses=frozenset({"completed"}))
    assert [h.quest_id for h in hits] == ["arch01"]
    assert hits[0].zone == "history"
    assert [o for o, _ in hits[0].events] == [1]


def test_events_search_archived_routes_through_store(monkeypatch, tmp_path: Path) -> None:
    class _FakeStore:
        def search_event_candidates(self, query):
            return [("arch01", "Archived one", "completed", "2026-06-11T00:00:00+00:00")]

        def get_events(self, cid):
            return [_ev("e0", "tool_call", name="bash", input={"cmd": "GREPTARGET"})]

    monkeypatch.setattr("imp.cli.store.make_store", lambda control: _FakeStore())

    def _boom(*a, **k):
        raise AssertionError("client path must not open the archive index directly")

    monkeypatch.setattr("imp.engine.archive.read.search_archive_events_content", _boom)

    hits = m._events_search_archived(tmp_path, "greptarget", types=None, agents=None,
                                     statuses=frozenset({"completed"}))
    assert [h.quest_id for h in hits] == ["arch01"]
    assert hits[0].zone == "history"
    assert hits[0].last_activity == "2026-06-11T00:00:00+00:00"
    assert hits[0].intent is None   # titled: no quest_card load (the fake has none)


def test_events_search_archived_keeps_only_selected_statuses(
        monkeypatch, tmp_path: Path) -> None:
    loaded: list[str] = []

    class _FakeStore:
        def search_event_candidates(self, query):
            return [("cmp01", "Completed one", "completed", None),
                    ("abn01", "Abandoned one", "abandoned", None),
                    ("arc01", "Archived one", "archived", None)]

        def get_events(self, cid):
            loaded.append(cid)
            return [_ev("e0", "tool_call", name="bash", input={"cmd": "GREPTARGET"})]

    monkeypatch.setattr("imp.cli.store.make_store", lambda control: _FakeStore())
    hits = m._events_search_archived(
        tmp_path, "greptarget", types=None, agents=None,
        statuses=frozenset({"abandoned", "archived"}))
    assert [h.quest_id for h in hits] == ["abn01", "arc01"]
    assert loaded == ["abn01", "arc01"]


def _hit(cid: str, title: str, zone: str, events: list[tuple[int, dict[str, Any]]],
         match_count: int, last: str = "2026-06-11T00:00:00+00:00") -> m.EventSearchHit:
    return m.EventSearchHit(cid, title, zone, last, events, {}, match_count, {}, None)


def _bounded_hits() -> list[m.EventSearchHit]:
    def events(cid: str) -> list[tuple[int, dict[str, Any]]]:
        return [
            (ordinal, _ev(f"{cid}-{ordinal}", "tool_call", "step:impl/coder",
                          name="bash",
                          input={"cmd": f"GREPTARGET {cid} event {ordinal}"}))
            for ordinal in range(1, 6)
        ]

    return [
        _hit("arc_old", "Archive Old", "history", events("arc_old"), 5,
             "2026-06-10T00:00:00+00:00"),
        _hit("act_old", "Active Old", "active", events("act_old"), 5,
             "2026-06-11T00:00:00+00:00"),
        _hit("arc_new", "Archive New", "history", events("arc_new"), 5,
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


@pytest.mark.parametrize(("flags", "hint"), [
    ({}, "Try another term."),
    ({"list": True}, "Try another term."),
    ({"completed": True, "type": ["tool_call"]},
     "Try another term, or drop --completed --type to widen the search."),
    ({"abandoned": True, "archived": True},
     "Try another term, or drop --abandoned --archived to widen the search."),
    ({"backlog": True, "agent": ["coder"]},
     "Try another term, or drop --backlog --agent to widen the search."),
    ({"active": True, "backlog": True, "completed": True, "abandoned": True,
      "archived": True}, "Try another term."),
    ({"active": True, "backlog": True, "completed": True, "abandoned": True,
      "archived": True, "type": ["tool_call"], "agent": ["coder"]},
     "Try another term, or drop --type --agent to widen the search."),
])
def test_cmd_events_search_no_match_headline_and_hint(
        monkeypatch, capsys, tmp_path: Path, flags: dict, hint: str) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(m, "_events_search", lambda *a, **k: [])
    rc = m._cmd_events_search(_search_ns("zzqq", **flags), tmp_path)
    assert rc == 0
    assert capsys.readouterr().out == f'No events match "zzqq".\n{hint}\n'


def test_cmd_events_search_no_match_dims_only_the_hint(
        monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setattr(m, "_events_search", lambda *a, **k: [])
    m._cmd_events_search(_search_ns("zzqq"), tmp_path)
    assert capsys.readouterr().out.splitlines() == [
        'No events match "zzqq".',
        pal.ansi("Try another term.", "dim", enabled=True),
    ]


def test_cmd_events_search_no_match_json_is_empty_array(
        monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setattr(m, "_events_search", lambda *a, **k: [])

    assert m._cmd_events_search(_search_ns("zzz", json=True), tmp_path) == 0

    assert capsys.readouterr().out == "[]\n"


def test_cmd_events_search_all_is_exhaustive_in_every_view(
        monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setattr(m, "_events_search", lambda *a, **k: _bounded_hits())
    quest_ids = ("act_new", "act_old", "bkl001", "arc_new", "arc_old")

    assert m._cmd_events_search(_search_ns("GREPTARGET", all=True), tmp_path) == 0
    rich = capsys.readouterr().out
    assert all(quest_id in rich for quest_id in quest_ids)
    assert "#5" in rich
    assert "more quests" not in rich
    assert "\n  … +" not in rich

    assert m._cmd_events_search(
        _search_ns("GREPTARGET", list=True, all=True), tmp_path) == 0
    listed = capsys.readouterr().out
    assert all(quest_id in listed for quest_id in quest_ids)

    assert m._cmd_events_search(
        _search_ns("GREPTARGET", json=True, all=True), tmp_path) == 0
    payload = _json.loads(capsys.readouterr().out)
    assert [hit["quest_id"] for hit in payload] == list(quest_ids)
    assert [len(hit["events"]) for hit in payload] == [5, 5, 0, 5, 5]


@pytest.mark.parametrize(
    ("options", "error"),
    [
        (["--limit", "0"], "imp: --limit must be a positive integer\n"),
        (["--limit", "-2"], "imp: --limit must be a positive integer\n"),
        (["--all", "--limit", "0"], "imp: --limit must be a positive integer\n"),
        (["--all", "--limit", "2"], "imp: --all and --limit cannot be used together\n"),
    ],
)
def test_main_events_search_rejects_invalid_output_controls_before_lookup(
        monkeypatch, capsys, tmp_path: Path, options: list[str], error: str) -> None:
    control_root_calls = 0

    def _record_control_root() -> Path:
        nonlocal control_root_calls
        control_root_calls += 1
        return tmp_path

    monkeypatch.setattr(m, "_control_root", _record_control_root)

    assert main(["events", "search", "GREPTARGET", *options]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == error
    assert control_root_calls == 0


def test_cmd_events_search_default_rich_is_bounded(
        monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setattr(m, "_events_search", lambda *a, **k: _bounded_hits())

    rc = m._cmd_events_search(_search_ns("GREPTARGET"), tmp_path)

    out = capsys.readouterr().out
    lines = out.splitlines()
    act_new_line = next(
        line for line in lines if line.startswith("act_new · Active New · active · "))
    act_old_index = next(
        index for index, line in enumerate(lines)
        if line.startswith("act_old · Active Old · active · "))
    act_new_index = lines.index(act_new_line)
    age = act_new_line.rsplit(" · ", 1)[1]

    assert rc == 0
    assert '21 matches in 5 quests for "GREPTARGET"' in out
    assert act_new_index < act_old_index < next(
        index for index, line in enumerate(lines)
        if line.startswith("bkl001 · Backlog One · backlog · "))
    assert age[:-1].isdigit() and age[-1] in {"s", "m", "h", "d"}
    assert [
        line.split()[0]
        for line in lines[act_new_index + 1:act_old_index]
        if line.startswith("  #")
    ] == ["#1", "#2", "#3"]
    assert "arc_new" not in out and "arc_old" not in out
    assert "#4" not in out
    assert out.count("  … +2 more") == 2
    assert "… +2 more quests" in out


def test_cmd_events_search_human_presentation_and_spacing(
        monkeypatch, tmp_path: Path) -> None:
    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    hits = _bounded_hits()
    hits[3] = hits[3]._replace(title="GREPTARGET Active New")
    monkeypatch.setattr(m, "_events_search", lambda *a, **k: hits)
    monkeypatch.setattr(m, "should_use_color", lambda stream: True)
    out = _Tty()
    monkeypatch.setattr(m.sys, "stdout", out)

    assert m._cmd_events_search(_search_ns("GREPTARGET"), tmp_path) == 0
    rich = out.getvalue()
    act_new_heading = next(
        line for line in rich.splitlines()
        if line.startswith("\x1b[33mact_new\x1b[0m · "))
    assert "\x1b[2m21 matches in 5 quests for \"GREPTARGET\"\x1b[0m" in rich
    assert act_new_heading.startswith(
        "\x1b[33mact_new\x1b[0m · "
        "\x1b[1m\x1b[33mGREPTARGET\x1b[0m Active New · "
        "\x1b[2mactive · "
    )
    assert "\x1b[2m  … +2 more\x1b[0m" in rich
    assert "\x1b[2m… +2 more quests\x1b[0m" in rich
    assert rich.endswith("\n\n") and not rich.endswith("\n\n\n")

    out.seek(0)
    out.truncate(0)
    assert m._cmd_events_search(
        _search_ns("GREPTARGET", list=True), tmp_path) == 0
    listed = out.getvalue()
    assert listed.removesuffix("\n\n").splitlines() == [
        (
            "\x1b[33mact_new\x1b[0m · "
            "\x1b[1m\x1b[33mGREPTARGET\x1b[0m Active New · "
            "\x1b[2mactive\x1b[0m"
        ),
        "\x1b[33mact_old\x1b[0m · Active Old · \x1b[2mactive\x1b[0m",
        "\x1b[33mbkl001\x1b[0m · Backlog One · \x1b[2mbacklog\x1b[0m",
        "\x1b[33marc_new\x1b[0m · Archive New · \x1b[2mhistory\x1b[0m",
        "\x1b[33marc_old\x1b[0m · Archive Old · \x1b[2mhistory\x1b[0m",
    ]
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


def test_main_routes_events_search_against_history(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    import json as _json
    line = _json.dumps({
        "id": "e0", "type": "tool_call",
        "payload": {"name": "bash", "input": {"cmd": "GREPTARGET"}}}) + "\n"
    _archive_events(tmp_path, "arch01", "Archived one", line)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["events", "search", "GREPTARGET", "--completed", "--no-pager"])
    out = buf.getvalue()
    assert rc == 0
    assert "arch01" in out
    assert '1 match in 1 quest for "GREPTARGET"' in out


def _seed_every_zone_and_status(tmp_path: Path) -> None:
    """GREPTARGET in act001 (active), bk0001 (backlog), and one history quest per status."""
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control
    line = _json.dumps({
        "id": "e0", "type": "tool_call", "timestamp": "2026-06-10T00:00:00+00:00",
        "payload": {"name": "bash", "input": {"cmd": "GREPTARGET"}}}) + "\n"
    for cid, status in (("cmp001", "completed"), ("abn001", "abandoned"),
                        ("arc001", "archived")):
        _archive_events(tmp_path, cid, f"History {cid}", line, status=status)
    _seed_control(tmp_path)
    q = Quest.create(tmp_path, title="Active one", quest_id="act001", base_branch="main")
    q.event_log().append(agent_id="step:impl/coder", type="tool_call",
                         payload={"name": "bash", "input": {"cmd": "GREPTARGET"}})
    Quest.create_draft(tmp_path, title="GREPTARGET backlog", quest_id="bk0001",
                       base_branch="main", origin="idea", seed_opening="a brief")


@pytest.mark.parametrize(("flags", "expected"), [
    ([], {"act001", "bk0001", "cmp001", "abn001", "arc001"}),
    (["--completed"], {"cmp001"}),
    (["--abandoned"], {"abn001"}),
    (["--archived"], {"arc001"}),
    (["--completed", "--abandoned"], {"cmp001", "abn001"}),
    (["--active", "--completed"], {"act001", "cmp001"}),
    (["--backlog", "--archived"], {"bk0001", "arc001"}),
    (["--active"], {"act001"}),
])
def test_main_events_search_zone_and_status_flags_select_hits(
        monkeypatch, capsys, tmp_path: Path, flags: list[str], expected: set[str]) -> None:
    _seed_every_zone_and_status(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_apply_theme", lambda: None)
    monkeypatch.setattr(m, "should_use_color", lambda stream: False)
    assert main(["events", "search", "GREPTARGET", *flags, "--list", "--no-pager"]) == 0
    out = capsys.readouterr().out
    assert {ln.split(" · ")[0] for ln in out.splitlines() if ln} == expected


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


def test_events_search_help_exits_zero_with_bounded_contract(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit) as exc:
        main(["events", "search", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Show at most LIMIT matched quests (default: 3, except with --list)." in out
    assert "Show every matched quest and matching event; conflicts with --limit." in out
    assert ("One terse line per matched quest (id, title, zone); combine with --limit to cap."
            in out)
    assert "Structured output, bounded like the default view unless --all." in out


def test_search_hit_digest_applies_codex_usage_patch_overlay(
        monkeypatch, capsys, tmp_path: Path) -> None:
    agent_id = "step:impl/coder"
    events = [
        _ev("msg1", "agent_message", agent_id,
            provider="codex", model="gpt-6-astra",
            stop_reason="end_turn", usage={"input_tokens": 100, "output_tokens": 50},
            content=[{"type": "text", "text": "GREPTARGET work done"}]),
        _ev("pat1", "agent_message_usage_patch", agent_id,
            context_tokens=8000, context_window=200000),
    ]
    matched, handle_map, overlays = m._matched_events(
        events, "greptarget", types=None, agents=None)
    assert overlays, "patch must produce an overlay for the preceding agent_message"
    hit = m.EventSearchHit("q1", "T1", "active", "2026-06-11T00:00:00+00:00",
                           matched, handle_map, len(matched), overlays, None)
    monkeypatch.setattr(m, "_events_search", lambda *a, **k: [hit])

    assert m._cmd_events_search(_search_ns("greptarget"), tmp_path) == 0
    out = capsys.readouterr().out
    assert "ctx=8000/160000 (5.0%)" in out


_OPENING = "/home/jpoimboe/foo.py -- in one sentence, what does this file do?"
_BRIEF = "Brief foo.py one\n\nmore body"


def _intent_line(text: str) -> str:
    return _json.dumps({
        "id": "open", "agent_id": None, "timestamp": "2026-06-10T00:00:00+00:00",
        "type": "user_message",
        "payload": {"subtype": "intent", "content": [{"type": "text", "text": text}]},
    }) + "\n"


def _titleless_in_every_zone(tmp_path: Path) -> None:
    """Titleless act001 (intent event), bk0001 (backlog brief), arc001 (archived, intent event)."""
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control
    _archive_events(tmp_path, "arc001", "", _intent_line(_OPENING), status="archived")
    _seed_control(tmp_path)
    q = Quest.create(tmp_path, title="", quest_id="act001", base_branch="main")
    q.event_log().append(
        agent_id=None, type="user_message",
        payload={"subtype": "intent", "content": [{"type": "text", "text": _OPENING}]})
    Quest.create_draft(tmp_path, title="", quest_id="bk0001", base_branch="main",
                       origin="idea", seed_opening=_BRIEF)


def test_events_search_hits_carry_the_titleless_name_source_in_every_zone(
        tmp_path: Path) -> None:
    _titleless_in_every_zone(tmp_path)
    hits = {h.quest_id: h for h in m._events_search(
        tmp_path, "foo.py",
        SearchZones(True, True, frozenset({"completed", "abandoned", "archived"}), None),
        types=None, agents=None)}
    assert hits["act001"].intent == _OPENING
    assert hits["bk0001"].intent == _BRIEF
    assert hits["arc001"].intent == _OPENING


def test_events_search_active_titleless_hit_falls_back_to_seed_opening(
        tmp_path: Path) -> None:
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control
    _seed_control(tmp_path)
    d = Quest.create_draft(tmp_path, title="", quest_id="drf001", base_branch="main",
                           seed_opening="seeded brief")
    d.event_log().append(agent_id="step:impl/coder", type="tool_call",
                         payload={"name": "bash", "input": {"cmd": "GREPTARGET"}})
    hits = m._events_search_active(tmp_path, "greptarget", types=None, agents=None)
    assert [(h.quest_id, h.intent) for h in hits] == [("drf001", "seeded brief")]


def test_events_search_archived_titleless_hit_falls_back_to_card_seed_opening(
        tmp_path: Path) -> None:
    # No intent event and no human turn: the name comes from the quest card's seed_opening.
    line = _json.dumps({
        "id": "e0", "type": "tool_call", "timestamp": "2026-06-10T00:00:00+00:00",
        "payload": {"name": "bash", "input": {"cmd": "GREPTARGET"}}}) + "\n"
    _archive_events(tmp_path, "arc003", "", line, status="archived",
                    seed_opening="seeded never-run brief")
    hits = m._events_search_archived(tmp_path, "greptarget", types=None, agents=None,
                                     statuses=frozenset({"archived"}))
    assert [(h.quest_id, h.intent) for h in hits] == [("arc003", "seeded never-run brief")]


def test_events_search_list_line_names_a_titleless_hit_dim() -> None:
    hit = m.EventSearchHit("bedo08", "", "history", None, [], {}, 1, {}, _OPENING)
    assert m._events_search_list_line(hit, query="zzz", color=False) == (
        f"bedo08 · {_OPENING} · history")
    assert m._events_search_list_line(hit, query="zzz", color=True) == (
        f"\x1b[33mbedo08\x1b[0m · \x1b[2m{_OPENING}\x1b[0m · \x1b[2mhistory\x1b[0m")


def test_events_search_list_line_clips_a_long_fallback_at_name_cap() -> None:
    opening = "summarize " + "y" * 100
    hit = m.EventSearchHit("bedo08", "", "history", None, [], {}, 1, {}, opening)
    assert m._events_search_list_line(hit, query="zzz", color=False) == (
        "bedo08 · summarize " + "y" * 59 + "… · history")


def test_events_search_list_line_omits_the_name_with_neither_title_nor_intent() -> None:
    hit = m.EventSearchHit("bedo08", "", "history", None, [], {}, 1, {}, None)
    assert m._events_search_list_line(hit, query="zzz", color=False) == "bedo08 · history"


def _headings(out: str) -> dict[str, str]:
    return {ln.split(" · ")[0]: ln for ln in out.splitlines()
            if " · " in ln and not ln.startswith(" ")}


def test_cmd_events_search_names_titleless_quests_in_every_zone(
        monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setattr(m, "should_use_color", lambda stream: False)
    _titleless_in_every_zone(tmp_path)
    for listed in (True, False):
        assert m._cmd_events_search(_search_ns("foo.py", list=listed), tmp_path) == 0
        heads = _headings(capsys.readouterr().out)
        assert heads["act001"].startswith(f"act001 · {_OPENING} · active")
        assert heads["bk0001"].startswith("bk0001 · Brief foo.py one more body · backlog")
        assert heads["arc001"].startswith(f"arc001 · {_OPENING} · history")


def test_events_search_list_line_highlights_the_query_over_the_dim_fallback() -> None:
    hit = m.EventSearchHit("bedo08", "", "history", None, [], {}, 1, {}, _OPENING)
    assert m._events_search_list_line(hit, query="foo.py", color=True) == (
        "\x1b[33mbedo08\x1b[0m · "
        "\x1b[2m/home/jpoimboe/\x1b[0m\x1b[1m\x1b[33mfoo.py\x1b[0m"
        "\x1b[2m -- in one sentence, what does this file do?\x1b[0m"
        " · \x1b[2mhistory\x1b[0m")


def test_events_search_help_copy_names_the_status_flags() -> None:
    p = m._build_events_search_parser()
    assert p.description == (
        "Case-insensitive substring search over structured event content (tool calls, "
        "tool results, agent reasoning, skill loads) across quests — the structured-event "
        "sibling of `imp search` (prose only). --active, --backlog, --completed, "
        "--abandoned, and --archived narrow the search and combine; with none of them, "
        "everything is searched. --type / --agent narrow which events match.")
    helps = {a.option_strings[-1]: a.help for a in p._actions if a.option_strings}
    assert helps["--completed"] == "Search completed quests' events."
    assert helps["--abandoned"] == "Search abandoned quests' events."
    assert helps["--archived"] == "Search archived conversations' events."


def test_events_search_rejects_the_retired_history_flag(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["events", "search", "q", "--history"])
    assert exc.value.code == 2
    assert "--history" in capsys.readouterr().err
