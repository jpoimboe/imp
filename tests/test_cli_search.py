from __future__ import annotations

import argparse
import json
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


def _archive(tmp_path: Path, quest_id: str, title: str) -> None:
    events = tmp_path / f"{quest_id}.jsonl"
    events.write_text("")
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


def _ns(query: str, *, active: bool = False, backlog: bool = False,
        archived: bool = False, list: bool = False,
        no_pager: bool = True) -> argparse.Namespace:
    return argparse.Namespace(
        query=query, active=active, backlog=backlog, archived=archived,
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
    assert "[archive]" in out                         # zone label, not status


def test_cmd_search_no_match_prints_notice(tmp_path: Path, capsys) -> None:
    rc = _cmd_search(_ns("nope"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "No Quests match" in out


def test_cmd_search_includes_matching_inflight_change(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Dark mode inflight",
                  quest_id="liveid", base_branch="main")
    rc = _cmd_search(_ns("dark"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "liveid" in out and "Dark mode inflight" in out
    assert "[active]" in out                           # in-flight is the active zone


def test_cmd_search_includes_matching_backlog_change(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(
        tmp_path, title="Flaky tracker", quest_id="bk1", base_branch="main",
        origin="idea", seed_opening="Flaky tracker\n\ntracking test_widget_timeout")
    rc = _cmd_search(_ns("test_widget_timeout"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "bk1" in out
    assert "[backlog]" in out


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
    # Default output carries an indented excerpt line containing the query term.
    assert any(ln.startswith("    ") and "300s" in ln
               for ln in default_out.splitlines())

    rc = _cmd_search(_ns("300s", list=True), tmp_path)
    list_out = capsys.readouterr().out
    assert rc == 0
    assert "liveid" in list_out and "[active]" in list_out
    assert not any(ln.startswith("    ") for ln in list_out.splitlines())  # no excerpts


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
