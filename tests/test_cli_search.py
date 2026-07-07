from __future__ import annotations

import argparse
import json
from pathlib import Path

from imp.cli.main import _build_parser, _cmd_search, _command_names
from imp.engine import archive


def test_search_is_a_subcommand() -> None:
    assert "search" in _command_names(_build_parser())


def test_search_parses_its_query() -> None:
    args = _build_parser().parse_args(["search", "dark mode"])
    assert args.query == "dark mode"
    assert args.func is _cmd_search


def _archive(tmp_path: Path, change_id: str, title: str) -> None:
    events = tmp_path / f"{change_id}.jsonl"
    events.write_text("")
    change_json = tmp_path / f"{change_id}.change.json"
    change_json.write_text(json.dumps({
        "id": change_id, "title": title, "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / f"{change_id}.state.json"
    state_json.write_text(json.dumps({"id": change_id, "status": "abandoned", "step": "spec"}))
    archive.archive_change(
        tmp_path, change_id, events_path=events,
        change_json_path=change_json, state_json_path=state_json)


def test_cmd_search_prints_matching_archived_change(tmp_path: Path, capsys) -> None:
    _archive(tmp_path, "darkid", "Dark mode toggle")
    _archive(tmp_path, "loginid", "Login flow")
    rc = _cmd_search(argparse.Namespace(query="dark"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "darkid" in out and "Dark mode toggle" in out
    assert "loginid" not in out                      # the query actually filters
    assert "[abandoned]" in out


def test_cmd_search_no_match_prints_notice(tmp_path: Path, capsys) -> None:
    rc = _cmd_search(argparse.Namespace(query="nope"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "No changes match" in out          # now covers in-flight + archived, not "archived" only


def test_cmd_search_includes_matching_inflight_change(tmp_path: Path, capsys) -> None:
    from imp.engine.change import Change
    from tests.conftest import _seed_control
    _seed_control(tmp_path)
    Change.create(tmp_path, title="Dark mode inflight",
                  change_id="liveid", base_branch="main")
    rc = _cmd_search(argparse.Namespace(query="dark"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "liveid" in out and "Dark mode inflight" in out   # in-flight title now visible
    assert "[new]" in out                                    # in-flight status is legible
