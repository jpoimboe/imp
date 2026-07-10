from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import imp.cli.main as m
from imp.engine.change import Change
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# _Row = (id, status, step, last_iso, title, created_at, step_label)
def _row(cid: str, last: str = "2026-06-11T00:00:00+00:00",
         ) -> tuple[str, str, str, str, str, str, str | None]:
    return (cid, "paused", "plan", last, "Title " + cid,
            "2026-06-01T00:00:00+00:00", None)


def test_abandon_match_rows_filters_by_token(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    for cid in ("lodo40", "lodo77", "tavo12"):
        Change.create_draft(tmp_path, title=cid, quest_id=cid,
                            base_branch="main", origin="idea")
    ids = {r[0] for r in m._abandon_match_rows(tmp_path, "lodo")}
    assert ids == {"lodo40", "lodo77"}


@pytest.mark.anyio
async def test_pick_abandon_matches_interactive_selection(
    monkeypatch, tmp_path: Path,
) -> None:
    # AC 15a: two-or-more live matches open the scoped abandon picker.
    monkeypatch.setattr(m, "_interactive", lambda: True)

    async def fake_picker(candidates, *, now, color, header, selected_index):
        assert "Abandon which change?" in header
        return candidates[1].quest_id

    monkeypatch.setattr(m.resume_select, "run_picker", fake_picker)
    rows = [_row("lodo40"), _row("lodo77")]
    assert await m._pick_abandon_matches(tmp_path, "lodo", rows) == "lodo77"


@pytest.mark.anyio
async def test_pick_abandon_matches_non_tty_token_specific(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    # AC 15d: non-tty ambiguity errors with a token-specific message distinct
    # from the no-token _abandon_picker_non_tty copy.
    monkeypatch.setattr(m, "_interactive", lambda: False)

    async def boom(*a, **k):
        raise AssertionError("picker drawn on a non-tty")

    monkeypatch.setattr(m.resume_select, "run_picker", boom)
    rows = [_row("lodo40"), _row("lodo77")]
    rc = await m._pick_abandon_matches(tmp_path, "lodo", rows)
    err = capsys.readouterr().err
    assert rc == 2
    assert "'lodo'" in err
    assert "more specific token" in err
    assert "abandon needs a change token" not in err   # distinct from no-token copy


def test_cmd_abandon_single_match_abandons_directly(
    monkeypatch, tmp_path: Path,
) -> None:
    # AC 15b
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_abandon_match_rows", lambda c, t: [_row("lodo40")])

    def boom_pick(*a, **k):
        raise AssertionError("picker invoked for a single match")

    def boom_resolve(*a, **k):
        raise AssertionError("_resolve_id invoked for a live match")

    monkeypatch.setattr(m, "_pick_abandon_matches", boom_pick)
    monkeypatch.setattr(m, "_resolve_id", boom_resolve)
    seen = {}

    def fake_abandon(control, quest_id, *, yes=False):
        seen["id"] = quest_id
        return 0

    monkeypatch.setattr(m, "_abandon", fake_abandon)
    rc = m._cmd_abandon(argparse.Namespace(token="lodo", yes=False))
    assert rc == 0
    assert seen["id"] == "lodo40"


def test_cmd_abandon_two_matches_open_picker(monkeypatch, tmp_path: Path) -> None:
    # AC 15a
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_abandon_match_rows",
                        lambda c, t: [_row("lodo40"), _row("lodo77")])

    async def fake_pick(control, token, rows):
        assert token == "lodo"
        return "lodo77"

    monkeypatch.setattr(m, "_pick_abandon_matches", fake_pick)
    seen = {}

    def fake_abandon(control, quest_id, *, yes=False):
        seen["id"] = quest_id
        return 0

    monkeypatch.setattr(m, "_abandon", fake_abandon)
    rc = m._cmd_abandon(argparse.Namespace(token="lodo", yes=False))
    assert rc == 0
    assert seen["id"] == "lodo77"


def test_cmd_abandon_two_matches_picker_rc_short_circuits(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_abandon_match_rows",
                        lambda c, t: [_row("lodo40"), _row("lodo77")])

    async def fake_pick(control, token, rows):
        return 2

    def boom_abandon(*a, **k):
        raise AssertionError("_abandon called despite picker rc")

    monkeypatch.setattr(m, "_pick_abandon_matches", fake_pick)
    monkeypatch.setattr(m, "_abandon", boom_abandon)
    assert m._cmd_abandon(argparse.Namespace(token="lodo", yes=False)) == 2


def test_cmd_abandon_zero_matches_archive_recovery(
    monkeypatch, tmp_path: Path,
) -> None:
    # AC 15c: token that resolves only in the archive still reaps its orphaned
    # branch (exact/suffix match against archived_ids).
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_abandon_match_rows", lambda c, t: [])

    def raise_exit(control, token):
        raise SystemExit(2)

    monkeypatch.setattr(m, "_resolve_id", raise_exit)
    monkeypatch.setattr(m.archive_read, "archived_ids", lambda c: ["gone40"])
    seen = {}

    def fake_abandon(control, quest_id, *, yes=False):
        seen["id"] = quest_id
        return 0

    monkeypatch.setattr(m, "_abandon", fake_abandon)
    rc = m._cmd_abandon(argparse.Namespace(token="gone40", yes=False))
    assert rc == 0
    assert seen["id"] == "gone40"


def test_cmd_abandon_zero_matches_unknown_exits_2(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_abandon_match_rows", lambda c, t: [])

    def raise_exit(control, token):
        raise SystemExit(2)

    def boom_abandon(*a, **k):
        raise AssertionError("_abandon called for an unknown token")

    monkeypatch.setattr(m, "_resolve_id", raise_exit)
    monkeypatch.setattr(m.archive_read, "archived_ids", lambda c: [])
    monkeypatch.setattr(m, "_abandon", boom_abandon)
    rc = m._cmd_abandon(argparse.Namespace(token="zzzz", yes=False))
    assert rc == 2
    assert "no Change matches" in capsys.readouterr().err
