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


@pytest.mark.anyio
async def test_log_picker_empty_prints_and_exits_2(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    monkeypatch.setattr(m, "_status_rows", lambda c: [])
    monkeypatch.setattr(m, "_completed_rows", lambda c: [])
    monkeypatch.setattr(m, "_resolve_default_id", lambda c: None)
    rc = await m._pick_log_target(tmp_path)
    out = capsys.readouterr().out
    assert rc == 2
    assert "No changes yet — start one with imp new" in out


@pytest.mark.anyio
async def test_log_picker_preselects_resolve_default_target(
    monkeypatch, tmp_path: Path,
) -> None:
    # in-flight is OLDER; a completed change is NEWER → newest-first puts the
    # completed change at index 0, but _resolve_default_id prefers the in-flight
    # one, so the pre-select must land on index 1 (AC #5 mixed case).
    inflight = _row("2026-06-10-inflit", "paused", "2026-06-10T00:00:00+00:00")
    completed = _row("2026-06-12-donexx", "completed", "2026-06-12T00:00:00+00:00")
    monkeypatch.setattr(m, "_status_rows", lambda c: [inflight])
    monkeypatch.setattr(m, "_completed_rows", lambda c: [completed])
    monkeypatch.setattr(m, "_resolve_default_id", lambda c: "2026-06-10-inflit")
    monkeypatch.setattr(m, "_interactive", lambda: True)
    captured = {}

    async def fake_picker(candidates, *, now, color, header, selected_index):
        captured["order"] = [c.change_id for c in candidates]
        captured["selected_index"] = selected_index
        return candidates[selected_index].change_id

    monkeypatch.setattr(m.resume_select, "run_picker", fake_picker)
    chosen = await m._pick_log_target(tmp_path)
    assert captured["order"] == ["2026-06-12-donexx", "2026-06-10-inflit"]
    assert captured["selected_index"] == 1
    assert chosen == "2026-06-10-inflit"


def test_cmd_log_non_tty_keeps_resolve_default(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: False)
    monkeypatch.setattr(m, "_resolve_default_id", lambda c: None)

    def boom(*a, **k):
        raise AssertionError("picker invoked on a non-tty")

    monkeypatch.setattr(m, "_pick_log_target", boom)
    rc = m._cmd_log(argparse.Namespace(token=None, full=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "No changes yet — start one with imp new" in out


def test_cmd_log_tty_no_token_routes_to_picker(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: True)

    async def fake_pick(control):
        return "2026-06-11-picked"

    monkeypatch.setattr(m, "_pick_log_target", fake_pick)
    seen = {}

    def fake_load(control, change_id):
        seen["id"] = change_id
        return None  # → "no such Change" rc 2, but proves the routing

    monkeypatch.setattr(m, "_load_events_for_id", fake_load)
    rc = m._cmd_log(argparse.Namespace(token=None, full=False))
    assert seen["id"] == "2026-06-11-picked"
    assert rc == 2


def test_cmd_log_tty_picker_rc_short_circuits(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_interactive", lambda: True)

    async def fake_pick(control):
        return 2

    monkeypatch.setattr(m, "_pick_log_target", fake_pick)

    def boom(*a, **k):
        raise AssertionError("load attempted despite picker rc")

    monkeypatch.setattr(m, "_load_events_for_id", boom)
    assert m._cmd_log(argparse.Namespace(token=None, full=False)) == 2


def test_cmd_log_with_token_skips_picker(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    def _raise(control, token):
        raise SystemExit(2)

    monkeypatch.setattr(m, "_resolve_id", _raise)

    def boom(*a, **k):
        raise AssertionError("picker invoked despite explicit token")

    monkeypatch.setattr(m, "_pick_log_target", boom)
    assert m._cmd_log(argparse.Namespace(token="bogus", full=False)) == 2
