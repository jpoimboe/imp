from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import imp.cli.main as m


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _row(cid: str, status: str = "paused",
         last: str = "2026-06-11T00:00:00+00:00",
         ) -> tuple[str, str, str, str, str, str, str | None]:
    return (cid, status, "plan", last, "Title " + cid,
            "2026-06-01T00:00:00+00:00", "planning")


def _events_ns(token: str | None = None, **over: object) -> argparse.Namespace:
    base: dict[str, object] = dict(
        token=token, range_pos=None, step=None, type=None, agent=None,
        range=None, limit=None, all=False, json=False, no_pager=True)
    base.update(over)
    return argparse.Namespace(**base)


@pytest.mark.anyio
async def test_pick_events_target_empty_prints_no_quests_and_exits_2(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    monkeypatch.setattr(m, "_status_rows", lambda c: [])
    monkeypatch.setattr(m, "_backlog_rows", lambda c: [])
    monkeypatch.setattr(m, "_completed_rows", lambda c: [])
    monkeypatch.setattr(m, "_resolve_default_id", lambda c: None)
    rc = await m._pick_events_target(tmp_path)
    out = capsys.readouterr().out
    assert rc == 2
    assert "No quests yet — start one with imp new" in out


@pytest.mark.anyio
async def test_pick_events_target_includes_backlog_and_preselects_default(
    monkeypatch, tmp_path: Path,
) -> None:
    # in-flight OLDEST, backlog middle, completed NEWEST → newest-first order is
    # completed, backlog, in-flight; _resolve_default_id prefers the in-flight one,
    # so the pre-select must land on index 2. The backlog row proves the pool
    # includes un-started backlog items (spec: "Picker pool (includes backlog)").
    inflight = _row("2026-06-10-inflit", "paused", "2026-06-10T00:00:00+00:00")
    backlog = _row("2026-06-11-backlg", "backlog", "2026-06-11T00:00:00+00:00")
    completed = _row("2026-06-12-donexx", "completed", "2026-06-12T00:00:00+00:00")
    monkeypatch.setattr(m, "_status_rows", lambda c: [inflight])
    monkeypatch.setattr(m, "_backlog_rows", lambda c: [backlog])
    monkeypatch.setattr(m, "_completed_rows", lambda c: [completed])
    monkeypatch.setattr(m, "_resolve_default_id", lambda c: "2026-06-10-inflit")
    monkeypatch.setattr(m, "_interactive", lambda: True)
    captured: dict[str, object] = {}

    async def fake_picker(candidates, *, now, color, header, selected_index):
        captured["order"] = [c.quest_id for c in candidates]
        captured["selected_index"] = selected_index
        captured["header"] = header
        return candidates[selected_index].quest_id

    monkeypatch.setattr(m.resume_select, "run_picker", fake_picker)
    chosen = await m._pick_events_target(tmp_path)
    assert captured["order"] == [
        "2026-06-12-donexx", "2026-06-11-backlg", "2026-06-10-inflit"]
    assert captured["selected_index"] == 2
    assert isinstance(captured["header"], str)
    assert captured["header"].startswith("Show events for which quest?")
    assert chosen == "2026-06-10-inflit"


@pytest.mark.anyio
async def test_pick_events_target_single_pool_skips_picker(
    monkeypatch, tmp_path: Path,
) -> None:
    only = _row("2026-06-10-only01")
    monkeypatch.setattr(m, "_status_rows", lambda c: [only])
    monkeypatch.setattr(m, "_backlog_rows", lambda c: [])
    monkeypatch.setattr(m, "_completed_rows", lambda c: [])
    monkeypatch.setattr(m, "_resolve_default_id", lambda c: "2026-06-10-only01")
    monkeypatch.setattr(m, "_interactive", lambda: True)

    def boom(*a, **k):
        raise AssertionError("run_picker drawn for a lone candidate")

    monkeypatch.setattr(m.resume_select, "run_picker", boom)
    chosen = await m._pick_events_target(tmp_path)
    assert chosen == "2026-06-10-only01"


@pytest.mark.anyio
async def test_pick_events_matches_scoped_rows_drop_unreadable(
    monkeypatch, tmp_path: Path,
) -> None:
    rows = {
        "2026-06-12-aa": _row("2026-06-12-aa", last="2026-06-12T00:00:00+00:00"),
        "2026-06-10-bb": _row("2026-06-10-bb", last="2026-06-10T00:00:00+00:00"),
    }
    monkeypatch.setattr(m, "_row_for_id", lambda c, cid: rows.get(cid))
    monkeypatch.setattr(m, "_interactive", lambda: True)
    captured: dict[str, object] = {}

    async def fake_picker(candidates, *, now, color, header, selected_index):
        captured["order"] = [c.quest_id for c in candidates]
        return candidates[0].quest_id

    monkeypatch.setattr(m.resume_select, "run_picker", fake_picker)
    chosen = await m._pick_events_matches(
        tmp_path, ["2026-06-10-bb", "2026-06-12-aa", "2026-06-13-unread"])
    assert captured["order"] == ["2026-06-12-aa", "2026-06-10-bb"]
    assert chosen == "2026-06-12-aa"


@pytest.mark.anyio
async def test_pick_events_matches_non_tty_prints_candidates_and_exits_2(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    monkeypatch.setattr(m, "_row_for_id", lambda c, cid: _row(cid))
    monkeypatch.setattr(m, "_interactive", lambda: False)
    rc = await m._pick_events_matches(
        tmp_path, ["2026-06-10-aa", "2026-06-11-bb"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "ambiguous token; candidates:" in err


def test_cmd_events_tty_no_token_routes_to_target_picker(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: True)

    async def fake_pick(control):
        return "2026-06-11-picked"

    monkeypatch.setattr(m, "_pick_events_target", fake_pick)
    monkeypatch.setattr(m, "_unstarted_backlog_meta", lambda c, i: None)
    seen: dict[str, str] = {}

    def fake_load(control, quest_id):
        seen["id"] = quest_id
        return None  # → "no such quest" rc 2, but proves the routing

    monkeypatch.setattr(m, "_load_events_for_id", fake_load)
    rc = m._cmd_events(_events_ns(token=None))
    assert seen["id"] == "2026-06-11-picked"
    assert rc == 2


def test_cmd_events_tty_no_token_picker_cancel_short_circuits(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: True)

    async def fake_pick(control):
        return 2  # picker cancel code

    monkeypatch.setattr(m, "_pick_events_target", fake_pick)

    def boom(*a, **k):
        raise AssertionError("load/backlog attempted despite picker cancel")

    monkeypatch.setattr(m, "_unstarted_backlog_meta", boom)
    monkeypatch.setattr(m, "_load_events_for_id", boom)
    assert m._cmd_events(_events_ns(token=None)) == 2


def test_cmd_events_non_tty_no_token_uses_resolve_default(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: False)

    def boom(*a, **k):
        raise AssertionError("picker invoked on a non-tty")

    monkeypatch.setattr(m, "_pick_events_target", boom)
    monkeypatch.setattr(m, "_resolve_default_id", lambda c: "2026-06-09-default")
    monkeypatch.setattr(m, "_unstarted_backlog_meta", lambda c, i: None)
    seen: dict[str, str] = {}

    def fake_load(control, quest_id):
        seen["id"] = quest_id
        return None

    monkeypatch.setattr(m, "_load_events_for_id", fake_load)
    rc = m._cmd_events(_events_ns(token=None))
    assert seen["id"] == "2026-06-09-default"
    assert rc == 2


def test_cmd_events_non_tty_no_quests_prints_message(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: False)
    monkeypatch.setattr(m, "_resolve_default_id", lambda c: None)

    def boom(*a, **k):
        raise AssertionError("picker invoked on a non-tty")

    monkeypatch.setattr(m, "_pick_events_target", boom)
    rc = m._cmd_events(_events_ns(token=None))
    out = capsys.readouterr().out
    assert rc == 0
    assert "No quests yet — start one with imp new" in out


def test_cmd_events_ambiguous_token_tty_routes_to_scoped_picker(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: True)
    monkeypatch.setattr(
        m, "_iter_quest_ids",
        lambda c: ["2026-06-10-foo", "2026-06-11-foo", "2026-06-12-bar"])

    def boom_resolve(*a, **k):
        raise AssertionError("_resolve_id invoked for an ambiguous tty token")

    monkeypatch.setattr(m, "_resolve_id", boom_resolve)
    captured: dict[str, object] = {}

    async def fake_matches(control, matches):
        captured["matches"] = matches
        return "2026-06-10-foo"

    monkeypatch.setattr(m, "_pick_events_matches", fake_matches)
    monkeypatch.setattr(m, "_unstarted_backlog_meta", lambda c, i: None)
    seen: dict[str, str] = {}

    def fake_load(control, quest_id):
        seen["id"] = quest_id
        return None

    monkeypatch.setattr(m, "_load_events_for_id", fake_load)
    rc = m._cmd_events(_events_ns(token="foo"))
    # picker receives exactly the substring matches (the "bar" id is excluded) —
    # the invariant that picker and _resolve_id agree on the candidate set.
    assert captured["matches"] == ["2026-06-10-foo", "2026-06-11-foo"]
    assert seen["id"] == "2026-06-10-foo"
    assert rc == 2


def test_cmd_events_ambiguous_token_non_tty_falls_through_to_resolve_id(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: False)
    monkeypatch.setattr(
        m, "_iter_quest_ids", lambda c: ["2026-06-10-foo", "2026-06-11-foo"])

    def raise_exit(control, token):
        raise SystemExit(2)  # real _resolve_id → _exit_ambiguous prints + exits 2

    monkeypatch.setattr(m, "_resolve_id", raise_exit)

    def boom(*a, **k):
        raise AssertionError("picker invoked on a non-tty ambiguous token")

    monkeypatch.setattr(m, "_pick_events_matches", boom)
    assert m._cmd_events(_events_ns(token="foo")) == 2


def test_cmd_events_single_match_token_resolves_without_picker(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: True)  # tty, yet no picker
    monkeypatch.setattr(
        m, "_iter_quest_ids", lambda c: ["2026-06-10-uniq", "2026-06-11-other"])
    monkeypatch.setattr(m, "_resolve_id", lambda c, t: "2026-06-10-uniq")

    def boom(*a, **k):
        raise AssertionError("picker invoked for a single-match token")

    monkeypatch.setattr(m, "_pick_events_matches", boom)
    monkeypatch.setattr(m, "_unstarted_backlog_meta", lambda c, i: None)
    seen: dict[str, str] = {}

    def fake_load(control, quest_id):
        seen["id"] = quest_id
        return None

    monkeypatch.setattr(m, "_load_events_for_id", fake_load)
    rc = m._cmd_events(_events_ns(token="uniq"))
    assert seen["id"] == "2026-06-10-uniq"
    assert rc == 2


def test_cmd_events_backlog_selection_prints_friendly_line_and_returns_0(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: False)
    monkeypatch.setattr(m, "_iter_quest_ids", lambda c: ["2026-06-10-back01"])
    monkeypatch.setattr(m, "_resolve_id", lambda c, t: "2026-06-10-back01")
    monkeypatch.setattr(
        m, "_unstarted_backlog_meta", lambda c, i: {"materialized": False})

    def boom(*a, **k):
        raise AssertionError("event load attempted for an un-started backlog quest")

    monkeypatch.setattr(m, "_load_events_for_id", boom)
    rc = m._cmd_events(_events_ns(token="back01"))
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "Quest 2026-06-10-back01 hasn't started yet — no events to show.\n"
