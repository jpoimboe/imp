from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import imp.cli.main as m
from imp.engine.quest import Quest
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _row(cid: str, status: str = "paused",
         last: str = "2026-06-11T00:00:00+00:00",
         ) -> tuple[str, str, str, str, str, str, str | None]:
    return (cid, status, "plan", last, "Title " + cid,
            "2026-06-01T00:00:00+00:00", "planning")


def _async_return(value):
    async def _coro(control):
        return value
    return _coro


@pytest.mark.anyio
async def test_abandon_picker_empty_prints_and_exits_2(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    monkeypatch.setattr(m, "_status_rows", lambda c: [])
    rc = await m._pick_abandon_target(tmp_path)
    out = capsys.readouterr().out
    assert rc == 2
    assert "Nothing to abandon." in out


@pytest.mark.anyio
async def test_abandon_picker_non_tty_refuses(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    monkeypatch.setattr(m, "_status_rows", lambda c: [_row("solo01")])
    monkeypatch.setattr(m, "_interactive", lambda: False)

    async def boom(*a, **k):
        raise AssertionError("picker drawn on a non-tty")

    monkeypatch.setattr(m.resume_select, "run_picker", boom)
    rc = await m._pick_abandon_target(tmp_path)
    err = capsys.readouterr().err
    assert rc == 2
    assert "non-interactive" in err


@pytest.mark.anyio
async def test_abandon_picker_tty_lone_auto_selects(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(m, "_status_rows", lambda c: [_row("solo01")])
    monkeypatch.setattr(m, "_interactive", lambda: True)

    async def boom(*a, **k):
        raise AssertionError("picker drawn for a lone candidate")

    monkeypatch.setattr(m.resume_select, "run_picker", boom)
    assert await m._pick_abandon_target(tmp_path) == "solo01"


@pytest.mark.anyio
async def test_abandon_picker_tty_multiple_uses_selection(
    monkeypatch, tmp_path: Path,
) -> None:
    rows = [_row("aaa111", last="2026-06-11T00:00:00+00:00"),
            _row("bbb222", last="2026-06-10T00:00:00+00:00")]
    monkeypatch.setattr(m, "_status_rows", lambda c: rows)
    monkeypatch.setattr(m, "_interactive", lambda: True)

    async def fake_picker(candidates, *, now, color, header, selected_index):
        assert "Abandon which change?" in header
        return candidates[1].quest_id

    monkeypatch.setattr(m.resume_select, "run_picker", fake_picker)
    assert await m._pick_abandon_target(tmp_path) == "bbb222"


def test_cmd_abandon_no_token_routes_through_picker(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_pick_abandon_target", _async_return("picked"))
    seen = {}

    def fake_abandon(control, quest_id, *, yes=False):
        seen["id"] = quest_id
        return 0

    monkeypatch.setattr(m, "_abandon", fake_abandon)
    rc = m._cmd_abandon(argparse.Namespace(token=None, yes=False))
    assert rc == 0
    assert seen == {"id": "picked"}


def test_cmd_abandon_no_token_picker_rc_short_circuits(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_pick_abandon_target", _async_return(2))

    def boom_abandon(*a, **k):
        raise AssertionError("_abandon called despite picker rc")

    monkeypatch.setattr(m, "_abandon", boom_abandon)
    assert m._cmd_abandon(argparse.Namespace(token=None, yes=False)) == 2


def test_cmd_abandon_with_token_skips_picker(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_resolve_id", lambda c, t: "resolved")

    def boom_picker(*a, **k):
        raise AssertionError("picker invoked despite explicit token")

    monkeypatch.setattr(m, "_pick_abandon_target", boom_picker)
    seen = {}

    def fake_abandon(control, quest_id, *, yes=False):
        seen["id"] = quest_id
        return 0

    monkeypatch.setattr(m, "_abandon", fake_abandon)
    rc = m._cmd_abandon(argparse.Namespace(token="resolved", yes=False))
    assert rc == 0
    assert seen["id"] == "resolved"


def test_abandon_rows_include_backlog_ideas(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Built", quest_id="feat", base_branch="main")
    Quest.create_draft(tmp_path, title="Idea one", quest_id="idea1",
                        base_branch="main", origin="idea")
    ids = {r[0] for r in m._abandon_rows(tmp_path)}
    assert {"feat", "idea1"} <= ids
