from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from imp.cli.main import _build_parser, _cmd_search, _command_names
from imp.engine import archive
from imp.engine.quest import Quest
from tests.conftest import _seed_control


def _user_msg_line(text: str) -> str:
    return json.dumps({
        "type": "user_message",
        "payload": {"content": [{"type": "text", "text": text}], "human": True},
    }) + "\n"


def _archive(tmp_path: Path, quest_id: str, title: str,
             status: str = "abandoned") -> None:
    events = tmp_path / f"{quest_id}.jsonl"
    events.write_text("")
    quest_json = tmp_path / f"{quest_id}.quest.json"
    quest_json.write_text(json.dumps({
        "id": quest_id, "title": title, "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / f"{quest_id}.state.json"
    state_json.write_text(json.dumps(
        {"id": quest_id, "status": status, "step": "spec"}))
    archive.archive_quest(
        tmp_path, quest_id, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)


def _archive_conv(tmp_path: Path, quest_id: str, title: str,
                  ts: str, text: str = "") -> None:
    events = tmp_path / f"{quest_id}.jsonl"
    events.write_text(json.dumps({
        "type": "user_message", "timestamp": ts,
        "payload": {"content": [{"type": "text", "text": text}], "human": True},
    }) + "\n")
    quest_json = tmp_path / f"{quest_id}.quest.json"
    quest_json.write_text(json.dumps({
        "id": quest_id, "title": title, "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / f"{quest_id}.state.json"
    state_json.write_text(json.dumps(
        {"id": quest_id, "status": "abandoned", "step": "spec"}))
    archive.archive_quest(
        tmp_path, quest_id, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)


def test_search_archived_hit_carries_last_activity(tmp_path: Path) -> None:
    from imp.cli.main import _search_archived
    _archive_conv(tmp_path, "darkid", "Dark mode", "2026-06-10T00:00:00+00:00",
                  "a dark note")
    hits = _search_archived(tmp_path, "dark")
    assert len(hits) == 1
    assert hits[0].last_activity == "2026-06-10T00:00:00+00:00"


def test_search_backlog_hit_carries_last_activity(tmp_path: Path) -> None:
    from imp.cli.main import _search_backlog
    _seed_control(tmp_path)
    draft = Quest.create_draft(
        tmp_path, title="Dark backlog", quest_id="darkbk", base_branch="main",
        origin="idea", seed_opening="Dark backlog")
    # A non-user_message event keeps the home in Backlog while dating it.
    draft.events_path.write_text(json.dumps({
        "type": "step_started", "timestamp": "2026-06-12T00:00:00+00:00",
        "payload": {}}) + "\n")
    hits = _search_backlog(tmp_path, "dark")
    assert hits and hits[0].last_activity == "2026-06-12T00:00:00+00:00"


def test_search_active_hit_carries_last_activity(tmp_path: Path) -> None:
    from imp.cli.main import _search_active
    _seed_control(tmp_path)
    live = Quest.create(tmp_path, title="Dark mode", quest_id="liveid",
                         base_branch="main")
    live.events_path.write_text(json.dumps({
        "type": "user_message", "timestamp": "2026-06-11T00:00:00+00:00",
        "payload": {"content": [{"type": "text", "text": "dark note"}],
                    "human": True}}) + "\n")
    hits = _search_active(tmp_path, "dark")
    assert hits and hits[0].last_activity == "2026-06-11T00:00:00+00:00"


def _ns(query: str, *, active: bool = False, backlog: bool = False,
        archived: bool = False, completed: bool = False,
        merged_after: str | None = None, list: bool = False,
        no_pager: bool = True) -> argparse.Namespace:
    return argparse.Namespace(
        query=query, active=active, backlog=backlog, archived=archived,
        completed=completed, merged_after=merged_after,
        list=list, no_pager=no_pager)


def test_search_is_a_subcommand() -> None:
    assert "search" in _command_names(_build_parser())


def test_search_parses_its_query_and_flags() -> None:
    args = _build_parser().parse_args(
        ["search", "dark mode", "--backlog", "-l", "--no-pager"])
    assert args.query == "dark mode"
    assert args.func is _cmd_search
    assert args.backlog is True and args.list is True and args.no_pager is True
    assert args.active is False and args.archived is False


def test_search_help_describes_zones_and_fields(capsys) -> None:
    with pytest.raises(SystemExit):           # --help prints then exits
        _build_parser().parse_args(["search", "--help"])
    text = capsys.readouterr().out.lower()
    assert "backlog" in text and "archived" in text
    assert "content" in text
    assert "archive only" not in text         # stale wording is gone


def test_cmd_search_prints_matching_archived_change(tmp_path: Path, capsys) -> None:
    _archive(tmp_path, "darkid", "Dark mode toggle")
    _archive(tmp_path, "loginid", "Login flow")
    rc = _cmd_search(_ns("dark"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "darkid" in out and "Dark mode toggle" in out
    assert "loginid" not in out
    assert "· archive ·" in out                       # zone label, not status


def test_cmd_search_no_match_prints_notice(tmp_path: Path, capsys) -> None:
    rc = _cmd_search(_ns("nope"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "No quests match" in out


def test_cmd_search_includes_matching_inflight_change(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Dark mode inflight",
                  quest_id="liveid", base_branch="main")
    rc = _cmd_search(_ns("dark"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "liveid" in out and "Dark mode inflight" in out
    assert "· active ·" in out                         # in-flight is the active zone


def test_cmd_search_includes_matching_backlog_change(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(
        tmp_path, title="Flaky tracker", quest_id="bk1", base_branch="main",
        origin="idea", seed_opening="Flaky tracker\n\ntracking test_widget_timeout")
    rc = _cmd_search(_ns("test_widget_timeout"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "bk1" in out
    assert "· backlog ·" in out


def test_cmd_search_zone_flag_scopes_to_backlog_only(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Dark active", quest_id="darkactive",
                  base_branch="main")
    Quest.create_draft(
        tmp_path, title="Dark backlog", quest_id="darkbk", base_branch="main",
        origin="idea", seed_opening="Dark backlog")
    _archive(tmp_path, "darkarc", "Dark archived")
    rc = _cmd_search(_ns("dark", backlog=True), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "darkbk" in out
    assert "darkactive" not in out and "darkarc" not in out


def test_cmd_search_default_shows_excerpt_but_list_is_terse(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    live = Quest.create(tmp_path, title="Quiet chat", quest_id="liveid",
                         base_branch="main")
    live.events_path.write_text(_user_msg_line("please wire a 300s deadline soon"))

    rc = _cmd_search(_ns("300s"), tmp_path)
    default_out = capsys.readouterr().out
    assert rc == 0
    assert any(ln.startswith("  ") and "300s" in ln
               for ln in default_out.splitlines())

    rc = _cmd_search(_ns("300s", list=True), tmp_path)
    list_out = capsys.readouterr().out
    assert rc == 0
    assert "liveid" in list_out and "· active ·" in list_out
    assert not any(ln.startswith("  ") for ln in list_out.splitlines())


def test_cmd_search_routes_through_pager(tmp_path: Path) -> None:
    _archive(tmp_path, "darkid", "Dark mode toggle")
    with patch("imp.cli.main._page_or_print") as page:
        rc = _cmd_search(_ns("dark", no_pager=False), tmp_path)
    assert rc == 0
    page.assert_called_once()
    assert page.call_args.kwargs["no_pager"] is False


def test_cmd_search_composes_two_zone_flags(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Dark active", quest_id="darkactive",
                  base_branch="main")
    Quest.create_draft(
        tmp_path, title="Dark backlog", quest_id="darkbk", base_branch="main",
        origin="idea", seed_opening="Dark backlog")
    _archive(tmp_path, "darkarc", "Dark archived")
    rc = _cmd_search(_ns("dark", active=True, backlog=True), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "darkactive" in out and "darkbk" in out    # both selected zones included
    assert "darkarc" not in out                       # archive excluded


def test_cmd_search_completed_zone_tags_and_filters(tmp_path: Path, capsys) -> None:
    _archive(tmp_path, "alpha", "Dark mode toggle", status="completed")
    _archive(tmp_path, "beta", "Dark login flow", status="abandoned")
    rc = _cmd_search(_ns("dark", completed=True, list=True), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "alpha" in out and "· completed ·" in out   # zone tag from _search_list_line
    assert "beta" not in out                           # abandoned excluded


def test_cmd_search_archive_wins_over_completed(tmp_path: Path, capsys) -> None:
    _archive(tmp_path, "alpha", "Dark mode toggle", status="completed")
    rc = _cmd_search(_ns("dark", archived=True, completed=True, list=True), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("alpha") == 1                     # not double-listed
    assert "· archive ·" in out and "· completed ·" not in out


def test_cmd_search_merged_after_without_completed_is_rejected(
        tmp_path: Path, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _cmd_search(_ns("dark", merged_after="abc123"), tmp_path)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--merged-after" in err and "--completed" in err


def test_search_parser_accepts_completed_and_merged_after() -> None:
    ns = _build_parser().parse_args(
        ["search", "dark", "--completed", "--merged-after", "abc123"])
    assert ns.completed is True
    assert ns.merged_after == "abc123"


def test_search_parser_defaults_completed_false_merged_after_none() -> None:
    ns = _build_parser().parse_args(["search", "dark"])
    assert ns.completed is False
    assert ns.merged_after is None


def test_search_help_mentions_completed_and_merged_after(capsys) -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["search", "--help"])
    out = capsys.readouterr().out
    out_flat = " ".join(out.split())
    assert "Search completed (merged) quests." in out
    assert "keep only quests whose" in out
    assert "--completed scopes to merged quests" in out_flat


def test_cmd_search_count_line_leads_plural(tmp_path: Path, capsys) -> None:
    _archive(tmp_path, "darkone", "Dark one")
    _archive(tmp_path, "darktwo", "Dark two")
    rc = _cmd_search(_ns("dark"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.splitlines()[0] == '2 matches for "dark"'


def test_cmd_search_count_line_singular(tmp_path: Path, capsys) -> None:
    _archive(tmp_path, "darkone", "Dark one")
    rc = _cmd_search(_ns("dark"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.splitlines()[0] == '1 match for "dark"'


def test_cmd_search_blank_line_separates_results(tmp_path: Path, capsys) -> None:
    _archive(tmp_path, "darkone", "Dark one")
    _archive(tmp_path, "darktwo", "Dark two")
    rc = _cmd_search(_ns("dark"), tmp_path)
    lines = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert lines[0] == '2 matches for "dark"'
    assert lines[1] == ""
    assert lines[3] == ""
    assert sum(1 for ln in lines if ln == "") == 2


def test_cmd_search_orders_within_zone_newest_first(tmp_path: Path, capsys) -> None:
    _archive_conv(tmp_path, "aaa", "Dark older", "2026-06-01T00:00:00+00:00")
    _archive_conv(tmp_path, "zzz", "Dark newer", "2026-06-20T00:00:00+00:00")
    rc = _cmd_search(_ns("dark"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.index("zzz") < out.index("aaa")


def test_cmd_search_archived_shows_last_activity_age(tmp_path: Path, capsys) -> None:
    _archive_conv(tmp_path, "darkid", "Dark mode", "2026-06-01T00:00:00+00:00")
    rc = _cmd_search(_ns("dark"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert re.search(r"· archive · \d", out)


def test_cmd_search_missing_timestamp_renders_dash_age(tmp_path: Path, capsys) -> None:
    _archive(tmp_path, "darkid", "Dark mode")
    rc = _cmd_search(_ns("dark"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "· archive · -" in out


def test_cmd_search_output_is_plain_when_color_off(tmp_path: Path, capsys) -> None:
    _archive(tmp_path, "darkid", "Dark mode toggle")
    rc = _cmd_search(_ns("dark"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "\x1b[" not in out
    assert "· archive ·" in out


def test_cmd_search_terse_is_headings_only(tmp_path: Path, capsys) -> None:
    _archive(tmp_path, "darkone", "Dark one")
    _archive(tmp_path, "darktwo", "Dark two")
    rc = _cmd_search(_ns("dark", list=True), tmp_path)
    lines = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert len(lines) == 2
    assert all(" · archive · " in ln for ln in lines)
