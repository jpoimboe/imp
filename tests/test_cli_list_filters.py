from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

import pytest

import imp.cli.main as m
from imp.cli.main import _build_parser, _cmd_list, _selected_list_dispositions, main
from imp.engine import archive
from imp.engine.quest import Quest
from tests.conftest import _seed_control


def _ns(**kw: bool) -> argparse.Namespace:
    base = {"active": False, "backlog": False, "merged": False,
            "abandoned": False, "archived": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_no_flag_selects_nothing() -> None:
    assert _selected_list_dispositions(_ns()) == set()


def test_single_flag_selects_that_disposition() -> None:
    assert _selected_list_dispositions(_ns(abandoned=True)) == {"abandoned"}
    assert _selected_list_dispositions(_ns(archived=True)) == {"archived"}
    assert _selected_list_dispositions(_ns(active=True)) == {"active"}
    assert _selected_list_dispositions(_ns(backlog=True)) == {"backlog"}


def test_flags_union_additively() -> None:
    assert _selected_list_dispositions(
        _ns(merged=True, abandoned=True)) == {"merged", "abandoned"}


_NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


def _archive_status(control: Path, cid: str, status: str,
                    *, last_ts: str = "2026-06-03T00:00:00+00:00") -> None:
    """Archive a quest (no live home) with the given final status + last-activity."""
    events = control / f"{cid}.jsonl"
    events.write_bytes(
        f'{{"id":"{cid}-e0","parent_id":null,"agent_id":null,'
        f'"timestamp":"{last_ts}","type":"step_started",'
        f'"payload":{{"id":"{cid}"}}}}\n'.encode())
    quest_json = control / f"{cid}-quest.json"
    quest_json.write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-02T00:00:00+00:00", "depends_on": [],
        "materialized": True}))
    state_json = control / f"{cid}-state.json"
    state_json.write_text(json.dumps({
        "id": cid, "status": status, "step": None, "merge_commit": "c0ffee"}))
    archive.archive_quest(
        control, cid, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)


def _ids(lines: list[str]) -> set[str]:
    return {ln.split()[0] for ln in lines if ln and ln[0].isalnum()}


def test_filtered_selects_only_the_requested_disposition(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "merged")
    _archive_status(tmp_path, "abnd", "abandoned")
    _archive_status(tmp_path, "shlv", "archived")
    lines = m._filtered_overview_lines(
        tmp_path, dispositions={"abandoned"}, cutoff=None, now=_NOW, color=False)
    out = "\n".join(lines)
    assert "abnd" in out
    assert "mrgd" not in out and "shlv" not in out


def test_filtered_unions_dispositions(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "merged")
    _archive_status(tmp_path, "abnd", "abandoned")
    _archive_status(tmp_path, "shlv", "archived")
    lines = m._filtered_overview_lines(
        tmp_path, dispositions={"merged", "abandoned", "archived"},
        cutoff=None, now=_NOW, color=False)
    assert _ids(lines) >= {"mrgd", "abnd", "shlv"}


def test_filtered_archived_selects_archived_status(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "shlv", "archived")
    _archive_status(tmp_path, "mrgd", "merged")
    lines = m._filtered_overview_lines(
        tmp_path, dispositions={"archived"}, cutoff=None, now=_NOW, color=False)
    out = "\n".join(lines)
    assert "shlv" in out and "mrgd" not in out


def test_filtered_cutoff_drops_stale_rows(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "recent", "merged", last_ts="2026-07-14T00:00:00+00:00")
    _archive_status(tmp_path, "stale", "merged", last_ts="2026-06-01T00:00:00+00:00")
    cutoff = datetime(2026, 7, 13, tzinfo=UTC)
    lines = m._filtered_overview_lines(
        tmp_path, dispositions={"merged"}, cutoff=cutoff, now=_NOW, color=False)
    out = "\n".join(lines)
    assert "recent" in out and "stale" not in out


def test_filtered_empty_returns_no_match_notice(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "merged")
    lines = m._filtered_overview_lines(
        tmp_path, dispositions={"abandoned"}, cutoff=None, now=_NOW, color=False)
    assert lines == ["No quests match that filter."]


def test_filtered_omits_empty_active_and_backlog_sections(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Live", quest_id="liveid", base_branch="main")
    _archive_status(tmp_path, "abnd", "abandoned")
    lines = m._filtered_overview_lines(
        tmp_path, dispositions={"abandoned"}, cutoff=None, now=_NOW, color=False)
    out = "\n".join(lines)
    assert "abnd" in out and "liveid" not in out
    assert "backlog" not in out


def test_default_overview_lines_unchanged_for_empty(tmp_path: Path) -> None:
    # The empty-quest early return still yields the no-quests sentinel unchanged.
    _seed_control(tmp_path)
    expected = ["No quests yet — start one with imp new"]
    assert m._overview_lines(
        tmp_path, show_all=False, now=_NOW, color=False) == expected


def test_list_parses_the_five_flags_and_since() -> None:
    args = _build_parser().parse_args(
        ["list", "--active", "--backlog", "--merged", "--abandoned",
         "--archived", "--since", "2d"])
    assert args.func is _cmd_list
    assert args.active and args.backlog and args.merged
    assert args.abandoned and args.archived
    assert args.since == "2d"


def test_list_defaults_flags_off_and_since_none() -> None:
    args = _build_parser().parse_args(["list"])
    assert not (args.active or args.backlog or args.merged
                or args.abandoned or args.archived)
    assert args.since is None


def _run_list(tmp_path, monkeypatch, argv) -> str:
    import imp.cli.main as mod
    monkeypatch.setattr(mod, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(argv) == 0
    return buf.getvalue()


def test_list_abandoned_shows_only_abandoned(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "merged")
    _archive_status(tmp_path, "abnd", "abandoned")
    out = _run_list(tmp_path, monkeypatch, ["list", "--abandoned"])
    assert "abnd" in out and "mrgd" not in out


def test_list_archived_shows_only_archived(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "merged")
    _archive_status(tmp_path, "abnd", "abandoned")
    _archive_status(tmp_path, "shlv", "archived")
    out = _run_list(tmp_path, monkeypatch, ["list", "--archived"])
    assert "shlv" in out and "mrgd" not in out and "abnd" not in out


def test_list_no_flag_matches_prints_no_match_notice(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "merged")
    out = _run_list(tmp_path, monkeypatch, ["list", "--archived"])
    assert out.strip() == "No quests match that filter."


def test_list_default_view_is_unfiltered(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Live", quest_id="liveid", base_branch="main")
    out = _run_list(tmp_path, monkeypatch, ["list"])
    assert "liveid" in out
    assert "No quests match that filter." not in out


def test_list_since_bad_value_exits_two_with_pinned_message(
        tmp_path, monkeypatch, capsys) -> None:
    import imp.cli.main as mod
    _seed_control(tmp_path)
    monkeypatch.setattr(mod, "_control_root", lambda: tmp_path)
    with pytest.raises(SystemExit) as exc:
        _cmd_list(_build_parser().parse_args(["list", "--since", "banana"]))
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert err == (
        "imp: --since expects 'today' or a number with a unit — "
        "h/d/w/m/y (e.g. 2d, 1w, 3m).\n")


def test_list_since_filters_stale_history(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "stale", "merged", last_ts="2020-01-01T00:00:00+00:00")
    out = _run_list(tmp_path, monkeypatch, ["list", "--since", "1d"])
    assert out.strip() == "No quests match that filter."


def test_list_since_only_defaults_to_include_merged(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    recent = datetime.now(UTC).isoformat()
    _archive_status(tmp_path, "freshm", "merged", last_ts=recent)
    out = _run_list(tmp_path, monkeypatch, ["list", "--since", "1d"])
    assert "freshm" in out


def test_list_default_routes_through_overview_lines(tmp_path, monkeypatch) -> None:
    import imp.cli.main as mod
    _seed_control(tmp_path)
    monkeypatch.setattr(mod, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    calls = {"overview": 0, "filtered": 0}
    real_overview = mod._overview_lines
    real_filtered = mod._filtered_overview_lines
    def spy_overview(*a, **k):
        calls["overview"] += 1
        return real_overview(*a, **k)
    def spy_filtered(*a, **k):
        calls["filtered"] += 1
        return real_filtered(*a, **k)
    monkeypatch.setattr(mod, "_overview_lines", spy_overview)
    monkeypatch.setattr(mod, "_filtered_overview_lines", spy_filtered)
    assert main(["list"]) == 0
    assert calls["overview"] == 1 and calls["filtered"] == 0


def test_list_help_names_archived_not_left(capsys) -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["list", "--help"])
    out = capsys.readouterr().out
    assert "abandoned/archived quests" in out                # list description
    assert "include abandoned and archived quests" in out    # -a/--all help
    assert "left" not in out


def test_search_help_names_history_not_left(capsys) -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["search", "--help"])
    out = capsys.readouterr().out
    assert "history (merged, abandoned, archived) quests" in " ".join(out.split())
    assert "left" not in out


def test_filtered_lists_archived_once(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "shlv", "archived")
    lines = m._filtered_overview_lines(
        tmp_path, dispositions={"merged", "abandoned", "archived"},
        cutoff=None, now=_NOW, color=False)
    body = [ln for ln in lines if ln and ln[0].isalnum()]
    assert [ln.split()[0] for ln in body].count("shlv") == 1


def test_all_history_lists_archived_once(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "merged")
    _archive_status(tmp_path, "shlv", "archived")
    lines = m._overview_lines(tmp_path, show_all=True, now=_NOW, color=False)
    body = [ln for ln in lines if ln and ln[0].isalnum()]
    ids = [ln.split()[0] for ln in body]
    assert ids.count("shlv") == 1
    assert ids.count("mrgd") == 1


def test_past_quest_count_excludes_archived(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "merged")
    _archive_status(tmp_path, "abnd", "abandoned")
    _archive_status(tmp_path, "shlv", "archived")
    assert m._past_quest_count(tmp_path) == 2   # merged + abandoned; archived is not a past quest


def _subparser(name: str) -> argparse.ArgumentParser:
    action = next(a for a in _build_parser()._actions
                  if isinstance(a, argparse._SubParsersAction))
    return action.choices[name]


def _flag_help(p: argparse.ArgumentParser, flag: str) -> str | None:
    return next(a.help for a in p._actions if flag in a.option_strings)


def test_list_help_copy_is_verbatim() -> None:
    p = _subparser("list")
    assert p.description == (
        "With no argument, an overview of your quests — in-flight, the backlog, "
        "and every merged quest. -a/--all also includes abandoned/archived quests "
        "(the block is then labelled `history`).")
    assert _flag_help(p, "--all") == (
        "Also include abandoned and archived quests "
        "(the block is then labelled `history`).")
    assert _flag_help(p, "--archived") == "Show only archived conversations."


def test_search_help_copy_is_verbatim() -> None:
    p = _subparser("search")
    assert (p.description or "").startswith(
        "Search active (in-flight + drafts), backlog, and history (merged, abandoned, "
        "archived) quests for the query as a case-insensitive substring of a quest's "
        "id, title, or conversation content.")
    assert _flag_help(p, "--archived") == "Search archived conversations."


@pytest.mark.parametrize("argv", [["list", "--shelved"], ["search", "q", "--shelved"]])
def test_retired_shelved_flag_is_rejected(argv: list[str], capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _build_parser().parse_args(argv)
    assert exc.value.code == 2
    assert "--shelved" in capsys.readouterr().err
