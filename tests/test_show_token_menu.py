from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import imp.cli.main as m


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# _Row = (id, status, step, last_iso, title, created_at, step_label[, _])
def _row(cid: str, last: str = "2026-06-11T00:00:00+00:00",
         ) -> tuple[str, str, str, str, str, str, str | None]:
    return (cid, "paused", "plan", last, "Title " + cid,
            "2026-06-01T00:00:00+00:00", None)


def test_show_all_rows_spans_pool_and_skips_unreadable(
    monkeypatch, tmp_path: Path,
) -> None:
    # AC 3: the no-token pool is the full _iter_quest_ids set (live + backlog +
    # archived); an id _row_for_id cannot read (None) is dropped from the menu.
    monkeypatch.setattr(m, "_iter_quest_ids",
                        lambda c: ["live10", "arch20", "bad30"])

    def fake_row(control: Path, cid: str):
        return None if cid == "bad30" else _row(cid)

    monkeypatch.setattr(m, "_row_for_id", fake_row)
    ids = {r[0] for r in m._show_all_rows(tmp_path)}
    assert ids == {"live10", "arch20"}


@pytest.mark.anyio
async def test_pick_show_target_empty_pool(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    # AC 8: bare `imp show` on an interactive tty with no changes prints
    # "No Quests to show." and returns 2, opening no menu.
    monkeypatch.setattr(m, "_show_all_rows", lambda c: [])
    monkeypatch.setattr(m, "_interactive", lambda: True)

    async def boom(*a, **k):
        raise AssertionError("picker drawn on an empty pool")

    monkeypatch.setattr(m.resume_select, "run_picker", boom)
    rc = await m._pick_show_target(tmp_path)
    assert rc == 2
    assert "No Quests to show." in capsys.readouterr().out


@pytest.mark.anyio
async def test_pick_show_matches_interactive_selection(
    monkeypatch, tmp_path: Path,
) -> None:
    # AC 1 / AC 9a: the scoped picker is headed "Show which Quest?" and returns
    # the selected id. Display rows are built from _row_for_id over the raw ids.
    monkeypatch.setattr(m, "_interactive", lambda: True)
    monkeypatch.setattr(m, "_row_for_id", lambda c, cid: _row(cid))

    async def fake_picker(candidates, *, now, color, header, selected_index):
        assert "Show which Quest?" in header
        return candidates[1].quest_id

    monkeypatch.setattr(m.resume_select, "run_picker", fake_picker)
    chosen = await m._pick_show_matches(tmp_path, ["lodo40", "lodo77"])
    assert chosen == "lodo77"


@pytest.mark.anyio
async def test_pick_show_matches_non_tty_lists_candidates(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    # Defensive parity: the scoped picker's on_non_tty lists the candidate rows
    # to stderr and returns 2 without drawing a menu (the real non-tty ambiguous
    # path routes through _resolve_id/_exit_ambiguous, so this is never reached).
    monkeypatch.setattr(m, "_interactive", lambda: False)
    monkeypatch.setattr(m, "_row_for_id", lambda c, cid: _row(cid))

    async def boom(*a, **k):
        raise AssertionError("picker drawn on a non-tty")

    monkeypatch.setattr(m.resume_select, "run_picker", boom)
    rc = await m._pick_show_matches(tmp_path, ["lodo40", "lodo77"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "lodo40" in err and "lodo77" in err


def _ns(token, resource=None):
    return argparse.Namespace(token=token, resource=resource, no_pager=True)


def test_cmd_show_two_matches_open_picker(monkeypatch, tmp_path: Path) -> None:
    # AC 1 / AC 9a: >=2 raw matches on an interactive tty open the scoped picker
    # with exactly those ids; _resolve_id is NOT consulted for the fork.
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_iter_quest_ids",
                        lambda c: ["lodo40", "lodo77", "tavo12"])
    monkeypatch.setattr(m, "_interactive", lambda: True)
    seen = {}

    async def fake_pick(control, matches):
        seen["matches"] = matches
        return 2  # short-circuit rc; dispatch must not run

    monkeypatch.setattr(m, "_pick_show_matches", fake_pick)

    def boom_resolve(*a, **k):
        raise AssertionError("_resolve_id used for an ambiguous interactive token")

    monkeypatch.setattr(m, "_resolve_id", boom_resolve)
    rc = m._cmd_show(_ns("lodo"))
    assert rc == 2
    assert seen["matches"] == ["lodo40", "lodo77"]


def test_cmd_show_picker_selection_dispatches(monkeypatch, tmp_path: Path) -> None:
    # AC 9b: an interactive selection of a scoped match flows into the resource
    # dispatch with that change id.
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_iter_quest_ids", lambda c: ["lodo40", "lodo77"])
    monkeypatch.setattr(m, "_interactive", lambda: True)

    async def fake_pick(control, matches):
        return "lodo77"

    monkeypatch.setattr(m, "_pick_show_matches", fake_pick)
    monkeypatch.setattr(m, "_unstarted_backlog_meta", lambda c, cid: None)
    seen = {}

    def fake_detail(control, quest_id, *, now):
        seen["id"] = quest_id
        return ["card"]

    monkeypatch.setattr(m, "_detail_card", fake_detail)
    monkeypatch.setattr(m, "_page_or_print", lambda lines, *, no_pager: None)
    rc = m._cmd_show(_ns("lodo"))
    assert rc == 0
    assert seen["id"] == "lodo77"


def test_cmd_show_single_match_skips_picker(monkeypatch, tmp_path: Path) -> None:
    # AC 6 / AC 9a: a token matching exactly one id resolves directly via
    # _resolve_id, opening no picker.
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_iter_quest_ids", lambda c: ["lodo40", "tavo12"])
    monkeypatch.setattr(m, "_interactive", lambda: True)

    def boom_pick(*a, **k):
        raise AssertionError("picker invoked for a single match")

    monkeypatch.setattr(m, "_pick_show_matches", boom_pick)
    monkeypatch.setattr(m, "_unstarted_backlog_meta", lambda c, cid: None)
    seen = {}

    def fake_detail(control, quest_id, *, now):
        seen["id"] = quest_id
        return []

    monkeypatch.setattr(m, "_detail_card", fake_detail)
    monkeypatch.setattr(m, "_page_or_print", lambda lines, *, no_pager: None)
    rc = m._cmd_show(_ns("lodo"))
    assert rc == 0
    assert seen["id"] == "lodo40"


def test_cmd_show_non_tty_ambiguous_exits_2(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    # AC 5 / AC 9c: a non-tty ambiguous token skips the picker and hits
    # _exit_ambiguous (candidate list to stderr, exit 2).
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_iter_quest_ids", lambda c: ["lodo40", "lodo77"])
    monkeypatch.setattr(m, "_interactive", lambda: False)

    def boom_pick(*a, **k):
        raise AssertionError("picker invoked on a non-tty")

    monkeypatch.setattr(m, "_pick_show_matches", boom_pick)
    monkeypatch.setattr(m, "_row_for_id", lambda c, cid: None)
    rc = m._cmd_show(_ns("lodo"))
    err = capsys.readouterr().err
    assert rc == 2
    assert "is ambiguous; candidates:" in err


def test_cmd_show_picker_cancel_no_dispatch(monkeypatch, tmp_path: Path) -> None:
    # AC 4: cancelling returns the picker rc and reaches no resource-dispatch code.
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_iter_quest_ids", lambda c: ["lodo40", "lodo77"])
    monkeypatch.setattr(m, "_interactive", lambda: True)

    async def cancel_pick(control, matches):
        return 2

    monkeypatch.setattr(m, "_pick_show_matches", cancel_pick)

    def boom(*a, **k):
        raise AssertionError("resource dispatch reached after cancel")

    monkeypatch.setattr(m, "_unstarted_backlog_meta", boom)
    monkeypatch.setattr(m, "_detail_card", boom)
    assert m._cmd_show(_ns("lodo")) == 2


def test_cmd_show_no_token_interactive_picker(monkeypatch, tmp_path: Path) -> None:
    # AC 2 / AC 9d: bare `imp show` on an interactive tty opens the no-token
    # picker; the selection flows into dispatch.
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: True)

    async def fake_pick(control):
        return "lodo40"

    monkeypatch.setattr(m, "_pick_show_target", fake_pick)
    monkeypatch.setattr(m, "_unstarted_backlog_meta", lambda c, cid: None)
    seen = {}

    def fake_detail(control, quest_id, *, now):
        seen["id"] = quest_id
        return []

    monkeypatch.setattr(m, "_detail_card", fake_detail)
    monkeypatch.setattr(m, "_page_or_print", lambda lines, *, no_pager: None)
    rc = m._cmd_show(_ns(None))
    assert rc == 0
    assert seen["id"] == "lodo40"


def test_cmd_show_empty_token_skips_picker_and_errors(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    # Keeps picker/resolve fork consistent with _resolve_id's empty-token guard,
    # regardless of change count.
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_iter_quest_ids", lambda c: ["lodo40", "lodo77"])
    monkeypatch.setattr(m, "_interactive", lambda: True)

    def boom_pick(*a, **k):
        raise AssertionError("picker must not open for an empty token")

    monkeypatch.setattr(m, "_pick_show_matches", boom_pick)
    rc = m._cmd_show(_ns(""))
    err = capsys.readouterr().err
    assert rc == 2
    assert "no Quest matches" in err


def test_cmd_show_no_token_non_tty_errors(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    # AC 5 / AC 9d: bare `imp show` on a non-tty errors asking for an id, exit 2,
    # opening no picker.
    def boom_control_root() -> object:
        raise AssertionError("_control_root called before the needs-a-change-id check")

    monkeypatch.setattr(m, "_control_root", boom_control_root)
    monkeypatch.setattr(m, "_interactive", lambda: False)

    def boom(*a, **k):
        raise AssertionError("picker invoked on a non-tty")

    monkeypatch.setattr(m, "_pick_show_target", boom)
    rc = m._cmd_show(_ns(None))
    err = capsys.readouterr().err
    assert rc == 2
    assert "needs a Quest id" in err
