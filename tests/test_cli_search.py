from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from imp.cli import palette as pal
from imp.cli.main import _build_parser, _cmd_search, _command_names
from imp.create import create_draft_quest
from imp.engine import archive
from imp.engine.quest import Quest
from tests.conftest import _seed_control
from tests.test_cli_search_helpers import _init_git_commits


def _user_msg_line(text: str) -> str:
    return json.dumps({
        "type": "user_message",
        "payload": {"content": [{"type": "text", "text": text}], "subtype": "reply"},
    }) + "\n"


def _archive(tmp_path: Path, quest_id: str, title: str,
             status: str = "abandoned", merge_commit: str | None = None) -> None:
    events = tmp_path / f"{quest_id}.jsonl"
    events.write_text("")
    quest_json = tmp_path / f"{quest_id}.quest.json"
    quest_json.write_text(json.dumps({
        "id": quest_id, "title": title, "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    state_json = tmp_path / f"{quest_id}.state.json"
    state_json.write_text(json.dumps(
        {"id": quest_id, "status": status, "step": "spec",
         "merge_commit": merge_commit}))
    archive.archive_quest(
        tmp_path, quest_id, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)


def _archive_conv(tmp_path: Path, quest_id: str, title: str,
                  ts: str, text: str = "") -> None:
    events = tmp_path / f"{quest_id}.jsonl"
    events.write_text(json.dumps({
        "type": "user_message", "timestamp": ts,
        "payload": {"content": [{"type": "text", "text": text}], "subtype": "reply"},
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
    from imp.cli.store import _search_archived
    _archive_conv(tmp_path, "darkid", "Dark mode", "2026-06-10T00:00:00+00:00",
                  "a dark note")
    hits = _search_archived(tmp_path, "dark",
                            {"completed", "abandoned", "archived"}, None)
    assert len(hits) == 1
    assert hits[0].last_activity == "2026-06-10T00:00:00+00:00"


def test_search_backlog_hit_carries_last_activity(tmp_path: Path) -> None:
    from imp.cli.store import _search_backlog
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
    from imp.cli.store import _search_active
    _seed_control(tmp_path)
    live = Quest.create(tmp_path, title="Dark mode", quest_id="liveid",
                         base_branch="main")
    live.events_path.write_text(json.dumps({
        "type": "user_message", "timestamp": "2026-06-11T00:00:00+00:00",
        "payload": {"content": [{"type": "text", "text": "dark note"}],
                    "subtype": "reply"}}) + "\n")
    hits = _search_active(tmp_path, "dark")
    assert hits and hits[0].last_activity == "2026-06-11T00:00:00+00:00"


def _ns(query: str, *, active: bool = False, backlog: bool = False,
        archived: bool = False, completed: bool = False, abandoned: bool = False,
        merged_after: str | None = None,
        list: bool = False, no_pager: bool = True) -> argparse.Namespace:
    return argparse.Namespace(
        query=query, active=active, backlog=backlog, archived=archived,
        completed=completed, abandoned=abandoned,
        merged_after=merged_after, list=list, no_pager=no_pager)


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
    assert "· history ·" in out                       # zone label, not status


_EVENTS_HINT = "Try another term, or `imp events search` to include tool calls and results."
_ALL_ZONES = dict(active=True, backlog=True, completed=True, abandoned=True, archived=True)


@pytest.mark.parametrize(("flags", "hint"), [
    ({}, _EVENTS_HINT),
    ({"list": True}, _EVENTS_HINT),
    ({"backlog": True}, "Try another term, or drop --backlog to widen the search."),
    ({"active": True, "merged_after": "abc123"},
     "Try another term, or drop --active --merged-after to widen the search."),
    ({"completed": True, "archived": True},
     "Try another term, or drop --completed --archived to widen the search."),
    (_ALL_ZONES, "Try another term."),
    ({**_ALL_ZONES, "merged_after": "abc123"},
     "Try another term, or drop --merged-after to widen the search."),
])
def test_cmd_search_no_match_headline_and_hint(
        tmp_path: Path, capsys, monkeypatch, flags: dict, hint: str) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    rc = _cmd_search(_ns("zzzqqq", **flags), tmp_path)
    assert rc == 0
    assert capsys.readouterr().out == f'No quests match "zzzqqq".\n{hint}\n'


def test_cmd_search_no_match_dims_only_the_hint(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    _cmd_search(_ns("zzzqqq"), tmp_path)
    assert capsys.readouterr().out.splitlines() == [
        'No quests match "zzzqqq".',
        pal.ansi(_EVENTS_HINT, "dim", enabled=True),
    ]


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


def test_cmd_search_merged_filters_to_completed(tmp_path: Path, capsys) -> None:
    _archive(tmp_path, "alpha", "Dark mode toggle", status="completed")
    _archive(tmp_path, "beta", "Dark login flow", status="abandoned")
    rc = _cmd_search(_ns("dark", completed=True, list=True), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "alpha" in out and "· history ·" in out    # history zone tag from _search_list_line
    assert "beta" not in out                           # abandoned excluded


def test_cmd_search_archived_filters_to_archived(tmp_path: Path, capsys) -> None:
    _archive(tmp_path, "shlv", "Dark shelved chat", status="archived")
    _archive(tmp_path, "done", "Dark merged work", status="completed")
    rc = _cmd_search(_ns("dark", archived=True, list=True), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "shlv" in out and "done" not in out
    assert "· history ·" in out


def test_cmd_search_merged_after_alone_enables_completed(tmp_path: Path, capsys) -> None:
    # No guard, no exit: --merged-after self-enables the completed disposition.
    _archive(tmp_path, "alpha", "Dark mode toggle", status="completed")
    rc = _cmd_search(_ns("dark", merged_after="deadbeef", list=True), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    # An unresolvable base commit fails-safe to excluding the hit (no crash).
    assert "No quests match" in out or "alpha" not in out


def test_cmd_search_merged_after_returns_completed_hit_when_resolvable(
        tmp_path: Path, capsys) -> None:
    sha_a, sha_b = _init_git_commits(tmp_path)
    _archive(tmp_path, "alpha", "Dark mode toggle", status="completed",
             merge_commit=sha_b)
    rc = _cmd_search(_ns("dark", merged_after=sha_a, list=True), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "alpha" in out            # merged-after self-enabled merged; hit kept


def test_cmd_search_completed_selects_merged_and_unmerged_completions(
        tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Dark live", quest_id="darklive", base_branch="main")
    Quest.create_draft(tmp_path, title="Dark backlog", quest_id="darkbk",
                       base_branch="main", origin="idea", seed_opening="Dark backlog")
    _archive(tmp_path, "cmpmrg", "Dark merged", status="completed", merge_commit="c0ffee")
    _archive(tmp_path, "cmpnom", "Dark unmerged", status="completed")
    _archive(tmp_path, "abnd", "Dark dropped", status="abandoned")
    _archive(tmp_path, "shlv", "Dark parked", status="archived")
    rc = _cmd_search(_ns("dark", completed=True, list=True), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "cmpmrg" in out and "cmpnom" in out
    for other in ("darklive", "darkbk", "abnd", "shlv"):
        assert other not in out, other


def test_cmd_search_merged_after_excludes_a_no_merge_completion(
        tmp_path: Path, capsys) -> None:
    sha_a, sha_b = _init_git_commits(tmp_path)
    _archive(tmp_path, "alpha", "Dark merged", status="completed", merge_commit=sha_b)
    _archive(tmp_path, "beta", "Dark unmerged", status="completed")
    _archive(tmp_path, "gamma", "Dark dropped", status="abandoned")
    rc = _cmd_search(_ns("dark", merged_after=sha_a, list=True), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "alpha" in out
    assert "beta" not in out and "gamma" not in out


def test_search_parser_accepts_merged_abandoned_archived_and_merged_after() -> None:
    ns = _build_parser().parse_args(
        ["search", "dark", "--completed", "--abandoned", "--archived",
         "--merged-after", "abc123"])
    assert ns.completed is True and ns.abandoned is True and ns.archived is True
    assert ns.merged_after == "abc123"
    assert not hasattr(ns, "merged")


def test_search_parser_defaults_flags_false_merged_after_none() -> None:
    ns = _build_parser().parse_args(["search", "dark"])
    assert ns.completed is False and ns.abandoned is False and ns.archived is False
    assert ns.merged_after is None


def test_search_help_mentions_completed_and_merged_after(capsys) -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["search", "--help"])
    out = capsys.readouterr().out
    out_flat = " ".join(out.split())
    assert "Search completed quests." in out
    assert "implies --completed" in out_flat
    assert "with none of them, everything is searched" in out_flat


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
    assert re.search(r"· history · \d", out)


def test_cmd_search_default_includes_no_merge_completion(tmp_path: Path, capsys) -> None:
    _archive(tmp_path, "dn1", "Dark done work", status="completed")
    rc = _cmd_search(_ns("Dark done"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "dn1" in out
    assert "· history ·" in out


def test_cmd_search_missing_timestamp_renders_dash_age(tmp_path: Path, capsys) -> None:
    _archive(tmp_path, "darkid", "Dark mode")
    rc = _cmd_search(_ns("dark"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "· history · -" in out


def test_cmd_search_output_is_plain_when_color_off(tmp_path: Path, capsys) -> None:
    _archive(tmp_path, "darkid", "Dark mode toggle")
    rc = _cmd_search(_ns("dark"), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "\x1b[" not in out
    assert "· history ·" in out


def test_cmd_search_terse_is_headings_only(tmp_path: Path, capsys) -> None:
    _archive(tmp_path, "darkone", "Dark one")
    _archive(tmp_path, "darktwo", "Dark two")
    rc = _cmd_search(_ns("dark", list=True), tmp_path)
    lines = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert len(lines) == 2
    assert all(" · history · " in ln for ln in lines)


_OPENING = "/home/jpoimboe/foo.py -- in one sentence, what does this file do?"
_BRIEF = "Brief foo.py one\n\nmore body"
_ALL_ARCHIVED = {"completed", "abandoned", "archived"}


def _intent_event(text: str) -> dict[str, object]:
    return {"type": "user_message", "timestamp": "2026-06-10T00:00:00+00:00",
            "payload": {"subtype": "intent", "content": [{"type": "text", "text": text}]}}


def _archive_titleless(tmp_path: Path, quest_id: str, *, intent: str | None,
                       seed: str | None) -> None:
    events = tmp_path / f"{quest_id}.jsonl"
    events.write_text("" if intent is None else json.dumps(_intent_event(intent)) + "\n")
    meta: dict[str, object] = {
        "id": quest_id, "title": "", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}
    if seed is not None:
        meta["seed_opening"] = seed
    quest_json = tmp_path / f"{quest_id}.quest.json"
    quest_json.write_text(json.dumps(meta))
    state_json = tmp_path / f"{quest_id}.state.json"
    state_json.write_text(json.dumps({"id": quest_id, "status": "archived", "step": "spec"}))
    archive.archive_quest(
        tmp_path, quest_id, events_path=events,
        quest_json_path=quest_json, state_json_path=state_json)


def _with_intent(tmp_path: Path, quest_id: str, title: str, intent: str) -> None:
    q = Quest.create(tmp_path, title=title, quest_id=quest_id, base_branch="main")
    q.event_log().append(
        agent_id=None, type="user_message",
        payload={"subtype": "intent", "content": [{"type": "text", "text": intent}]})


def _titleless_in_every_zone(tmp_path: Path) -> str:
    """Titleless arc001 (archived, intent), arc002 (archived, seed only), act001
    (active, intent), bk0001 (backlog brief) and an unstarted draft; returns the draft id."""
    _archive_titleless(tmp_path, "arc001", intent=_OPENING, seed=None)
    _archive_titleless(tmp_path, "arc002", intent=None, seed="seed only brief")
    _seed_control(tmp_path)
    _with_intent(tmp_path, "act001", "", _OPENING)
    Quest.create_draft(tmp_path, title="", quest_id="bk0001", base_branch="main",
                       origin="idea", seed_opening=_BRIEF)
    return create_draft_quest(tmp_path, "summarize\n  bar.py").id


def test_search_hits_carry_the_titleless_name_source_in_every_zone(tmp_path: Path) -> None:
    from imp.cli.store import _search_active, _search_archived, _search_backlog
    draft_id = _titleless_in_every_zone(tmp_path)
    _with_intent(tmp_path, "ttl001", "Titled foo.py work", "an opening foo.py prompt")
    active = {h.quest_id: h.intent for h in _search_active(tmp_path, "foo.py")}
    assert active == {"act001": _OPENING, "ttl001": None}
    assert [h.intent for h in _search_active(tmp_path, draft_id)] == ["summarize\n  bar.py"]
    assert [(h.quest_id, h.intent) for h in _search_backlog(tmp_path, "foo.py")] == [
        ("bk0001", _BRIEF)]
    archived = {h.quest_id: h.intent
                for h in _search_archived(tmp_path, "foo.py", _ALL_ARCHIVED, None)}
    assert archived == {"arc001": _OPENING}
    assert [h.intent for h in _search_archived(tmp_path, "arc002", _ALL_ARCHIVED, None)] == [
        "seed only brief"]


def test_cmd_search_names_titleless_quests_in_every_zone(
        monkeypatch, tmp_path: Path, capsys) -> None:
    import imp.cli.main as m
    monkeypatch.setattr(m, "should_use_color", lambda stream: False)
    _titleless_in_every_zone(tmp_path)
    for listed in (True, False):
        assert _cmd_search(_ns("foo.py", list=listed), tmp_path) == 0
        heads = {ln.split(" · ")[0]: ln for ln in capsys.readouterr().out.splitlines()
                 if " · " in ln and not ln.startswith(" ")}
        assert heads["act001"].startswith(f"act001 · {_OPENING} · active · ")
        assert heads["bk0001"].startswith("bk0001 · Brief foo.py one more body · backlog · ")
        assert heads["arc001"].startswith(f"arc001 · {_OPENING} · history · ")
