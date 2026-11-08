from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

import pytest

import imp.cli.main as m
from imp.cli import palette as pal
from imp.cli.main import _build_parser, _cmd_list, _selected_list_dispositions, main
from imp.engine import archive
from imp.engine.quest import Quest
from tests.conftest import _command_summaries, _seed_control


def _ns(**kw: bool) -> argparse.Namespace:
    base = {"active": False, "backlog": False, "completed": False,
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
        _ns(completed=True, abandoned=True)) == {"completed", "abandoned"}


_NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


def _archive_status(control: Path, cid: str, status: str,
                    *, last_ts: str = "2026-06-03T00:00:00+00:00",
                    merge_commit: str | None = "c0ffee") -> None:
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
        "id": cid, "status": status, "step": None, "merge_commit": merge_commit}))
    archive.archive_quest(
        control, cid, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)


def _ids(lines: list[str]) -> set[str]:
    return {ln.split()[0] for ln in lines if ln and ln[0].isalnum()}


def test_filtered_selects_only_the_requested_disposition(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "completed")
    _archive_status(tmp_path, "abnd", "abandoned")
    _archive_status(tmp_path, "shlv", "archived")
    lines = m._filtered_overview_lines(
        tmp_path, dispositions={"abandoned"}, cutoff=None, now=_NOW, color=False)
    out = "\n".join(lines)
    assert "abnd" in out
    assert "mrgd" not in out and "shlv" not in out


def test_filtered_unions_dispositions(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "completed")
    _archive_status(tmp_path, "abnd", "abandoned")
    _archive_status(tmp_path, "shlv", "archived")
    lines = m._filtered_overview_lines(
        tmp_path, dispositions={"completed", "abandoned", "archived"},
        cutoff=None, now=_NOW, color=False)
    assert _ids(lines) >= {"mrgd", "abnd", "shlv"}


def test_filtered_archived_selects_archived_status(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "shlv", "archived")
    _archive_status(tmp_path, "mrgd", "completed")
    lines = m._filtered_overview_lines(
        tmp_path, dispositions={"archived"}, cutoff=None, now=_NOW, color=False)
    out = "\n".join(lines)
    assert "shlv" in out and "mrgd" not in out


def test_filtered_cutoff_drops_stale_rows(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "recent", "completed", last_ts="2026-07-14T00:00:00+00:00")
    _archive_status(tmp_path, "stale", "completed", last_ts="2026-06-01T00:00:00+00:00")
    cutoff = datetime(2026, 7, 13, tzinfo=UTC)
    lines = m._filtered_overview_lines(
        tmp_path, dispositions={"completed"}, cutoff=cutoff, now=_NOW, color=False)
    out = "\n".join(lines)
    assert "recent" in out and "stale" not in out


def test_filtered_empty_returns_no_match_notice(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "completed")
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
        ["list", "--active", "--backlog", "--completed", "--abandoned",
         "--archived", "--since", "2d"])
    assert args.func is _cmd_list
    assert args.active and args.backlog and args.completed
    assert args.abandoned and args.archived
    assert args.since == "2d"


def test_list_defaults_flags_off_and_since_none() -> None:
    args = _build_parser().parse_args(["list"])
    assert not (args.active or args.backlog or args.completed
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
    _archive_status(tmp_path, "mrgd", "completed")
    _archive_status(tmp_path, "abnd", "abandoned")
    out = _run_list(tmp_path, monkeypatch, ["list", "--abandoned"])
    assert "abnd" in out and "mrgd" not in out


def test_list_archived_shows_only_archived(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "completed")
    _archive_status(tmp_path, "abnd", "abandoned")
    _archive_status(tmp_path, "shlv", "archived")
    out = _run_list(tmp_path, monkeypatch, ["list", "--archived"])
    assert "shlv" in out and "mrgd" not in out and "abnd" not in out


def test_list_no_flag_matches_prints_no_match_notice(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "completed")
    out = _run_list(tmp_path, monkeypatch, ["list", "--archived"])
    assert out.strip() == "No quests match that filter."


def _seed_every_disposition(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Live", quest_id="liveid", base_branch="main")
    Quest.create_draft(tmp_path, title="Idea", quest_id="bklgid", base_branch="main",
                       origin="idea", seed_opening="Idea")
    _archive_status(tmp_path, "cmpmrg", "completed")
    _archive_status(tmp_path, "cmpnom", "completed", merge_commit=None)
    _archive_status(tmp_path, "abnd", "abandoned")
    _archive_status(tmp_path, "shlv", "archived")


def test_list_completed_shows_merged_and_unmerged_completions_only(
        tmp_path, monkeypatch) -> None:
    _seed_every_disposition(tmp_path)
    out = _run_list(tmp_path, monkeypatch, ["list", "--completed"])
    assert "cmpmrg" in out and "cmpnom" in out
    for other in ("liveid", "bklgid", "abnd", "shlv"):
        assert other not in out, other


def test_list_default_view_shows_both_kinds_of_completion(tmp_path, monkeypatch) -> None:
    _seed_every_disposition(tmp_path)
    out = _run_list(tmp_path, monkeypatch, ["list"])
    assert "cmpmrg" in out and "cmpnom" in out
    assert "abnd" not in out and "shlv" not in out


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
    _archive_status(tmp_path, "stale", "completed", last_ts="2020-01-01T00:00:00+00:00")
    out = _run_list(tmp_path, monkeypatch, ["list", "--since", "1d"])
    assert out.strip() == "No quests match that filter."


def test_list_since_only_defaults_to_include_merged(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    recent = datetime.now(UTC).isoformat()
    _archive_status(tmp_path, "freshm", "completed", last_ts=recent)
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
    assert "history (completed, abandoned, archived) quests" in " ".join(out.split())
    assert "left" not in out


def test_filtered_lists_archived_once(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "shlv", "archived")
    lines = m._filtered_overview_lines(
        tmp_path, dispositions={"completed", "abandoned", "archived"},
        cutoff=None, now=_NOW, color=False)
    body = [ln for ln in lines if ln and ln[0].isalnum()]
    assert [ln.split()[0] for ln in body].count("shlv") == 1


def test_all_history_lists_archived_once(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "completed")
    _archive_status(tmp_path, "shlv", "archived")
    lines = m._overview_lines(tmp_path, show_all=True, now=_NOW, color=False)
    body = [ln for ln in lines if ln and ln[0].isalnum()]
    ids = [ln.split()[0] for ln in body]
    assert ids.count("shlv") == 1
    assert ids.count("mrgd") == 1


def test_past_quest_count_excludes_archived(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "mrgd", "completed")
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
    assert _command_summaries()["list"] == (
        "List your quests: in-flight, backlog, and completed.")
    assert p.description == (
        "With no argument, an overview of your quests — in-flight, the backlog, "
        "and every completed quest. -a/--all also includes abandoned/archived quests "
        "(the block is then labelled `history`).")
    assert _flag_help(p, "--completed") == "Show only completed quests."
    assert _flag_help(p, "--all") == (
        "Also include abandoned and archived quests "
        "(the block is then labelled `history`).")
    assert _flag_help(p, "--archived") == "Show only archived conversations."


def test_search_help_copy_is_verbatim() -> None:
    p = _subparser("search")
    assert p.description == (
        "Search active (in-flight + drafts), backlog, and history (completed, abandoned, "
        "archived) quests for the query as a case-insensitive substring of a quest's "
        "id, title, or conversation content. --active, --backlog, --completed, "
        "--abandoned, and --archived narrow the search and combine; with none of "
        "them, everything is searched.")
    assert _flag_help(p, "--completed") == "Search completed quests."
    assert _flag_help(p, "--merged-after") == (
        "Keep only completed quests whose merge landed after COMMIT "
        "(implies --completed).")
    assert _flag_help(p, "--archived") == "Search archived conversations."


@pytest.mark.parametrize("argv", [["list", "--shelved"], ["search", "q", "--shelved"]])
def test_retired_shelved_flag_is_rejected(argv: list[str], capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _build_parser().parse_args(argv)
    assert exc.value.code == 2
    assert "--shelved" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["list", "--merged"],
    ["search", "q", "--merged"],
    ["search", "--merged", "abc", "q"],   # must not resolve as --merged-after abc
])
def test_retired_merged_flag_is_rejected(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        _build_parser().parse_args(argv)
    assert exc.value.code == 2


def _seed_backlog_and_history(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="Idea", quest_id="bklgid", base_branch="main",
                       origin="idea", seed_opening="Idea")
    _archive_status(tmp_path, "cmpmrg", "completed")


_HEADER_WORDS = ["ID", "TITLE", "STATUS", "UPDATED"]


def _assert_notice_then_header_then(lines: list[str], first_label: str) -> None:
    assert lines[:2] == ["no active quests", ""]
    assert lines[2].split() == _HEADER_WORDS
    assert lines[3] == first_label


@pytest.mark.parametrize(("argv", "history_label"), [
    (["list"], "completed:"),
    (["list", "-a"], "history:"),
])
def test_list_with_no_inflight_rows_says_so_above_the_header(
        tmp_path, monkeypatch, argv, history_label) -> None:
    _seed_backlog_and_history(tmp_path)
    lines = _run_list(tmp_path, monkeypatch, argv).splitlines()
    _assert_notice_then_header_then(lines, "backlog:")
    assert lines[lines.index(history_label) - 1] == ""


def test_no_active_notice_then_header_then_completed_when_backlog_is_empty(
        tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "cmpmrg", "completed")
    lines = _run_list(tmp_path, monkeypatch, ["list"]).splitlines()
    _assert_notice_then_header_then(lines, "completed:")


def test_no_active_notice_is_dim_under_color(tmp_path: Path) -> None:
    _seed_backlog_and_history(tmp_path)
    lines = m._overview_lines(tmp_path, show_all=False, now=_NOW, color=True)
    assert lines[0] == pal.ansi("no active quests", "dim", enabled=True)
    assert lines[0] != "no active quests"
    assert lines[1] == ""
    assert "STATUS" in lines[2]


def test_list_with_a_live_quest_has_no_notice(tmp_path, monkeypatch) -> None:
    _seed_backlog_and_history(tmp_path)
    Quest.create(tmp_path, title="Live", quest_id="liveid", base_branch="main")
    out = _run_list(tmp_path, monkeypatch, ["list"])
    assert out.splitlines()[0].split() == _HEADER_WORDS
    assert "liveid" in out
    assert "no active quests" not in out


def test_active_filter_with_other_zone_rows_shows_the_notice(tmp_path, monkeypatch) -> None:
    _seed_backlog_and_history(tmp_path)
    lines = _run_list(tmp_path, monkeypatch, ["list", "--active", "--backlog"]).splitlines()
    _assert_notice_then_header_then(lines, "backlog:")


def test_filter_without_active_never_shows_the_notice(tmp_path, monkeypatch) -> None:
    _seed_backlog_and_history(tmp_path)
    _archive_status(tmp_path, "abnd", "abandoned")
    out = _run_list(tmp_path, monkeypatch, ["list", "--abandoned"])
    assert "abnd" in out
    assert "no active quests" not in out


def test_active_filter_with_nothing_to_show_prints_only_the_sentinel(
        tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "cmpmrg", "completed")
    out = _run_list(tmp_path, monkeypatch, ["list", "--active"])
    assert out.strip() == "No quests match that filter."


def test_since_cutoff_suppresses_the_notice(tmp_path, monkeypatch) -> None:
    _seed_backlog_and_history(tmp_path)
    out = _run_list(tmp_path, monkeypatch, ["list", "--since", "today"])
    assert "bklgid" in out
    assert "no active quests" not in out


_HIDDEN_HISTORY = "No active quests — see imp list -a for history"


@pytest.mark.parametrize("status", ["abandoned", "archived"])
def test_list_with_only_hidden_history_points_at_dash_a(
        tmp_path, monkeypatch, status) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "gone", status)
    assert _run_list(tmp_path, monkeypatch, ["list"]).strip() == _HIDDEN_HISTORY
    assert m._overview_lines(
        tmp_path, show_all=False, now=_NOW, color=False) == [_HIDDEN_HISTORY]


def test_list_all_with_only_hidden_history_shows_it(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "abnd", "abandoned")
    lines = _run_list(tmp_path, monkeypatch, ["list", "-a"]).splitlines()
    _assert_notice_then_header_then(lines, "history:")
    assert lines[4].split()[0] == "abnd"


def test_list_archived_filter_has_no_blank_after_header(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    _archive_status(tmp_path, "shlv", "archived")
    lines = _run_list(tmp_path, monkeypatch, ["list", "--archived"]).splitlines()
    assert lines[1] == "history:"
    assert lines[2].split()[0] == "shlv"


def test_list_backlog_filter_has_no_blank_after_header(tmp_path, monkeypatch) -> None:
    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="Idea", quest_id="bklgid", base_branch="main",
                       origin="idea", seed_opening="Idea")
    lines = _run_list(tmp_path, monkeypatch, ["list", "--backlog"]).splitlines()
    assert lines[1] == "backlog:"
    assert lines[2].split()[0] == "bklgid"
