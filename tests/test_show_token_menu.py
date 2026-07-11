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
    # AC 3: the no-token pool is the full _iter_change_ids set (live + backlog +
    # archived); an id _row_for_id cannot read (None) is dropped from the menu.
    monkeypatch.setattr(m, "_iter_change_ids",
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
    # "No changes to show." and returns 2, opening no menu.
    monkeypatch.setattr(m, "_show_all_rows", lambda c: [])
    monkeypatch.setattr(m, "_interactive", lambda: True)

    async def boom(*a, **k):
        raise AssertionError("picker drawn on an empty pool")

    monkeypatch.setattr(m.resume_select, "run_picker", boom)
    rc = await m._pick_show_target(tmp_path)
    assert rc == 2
    assert "No changes to show." in capsys.readouterr().out


@pytest.mark.anyio
async def test_pick_show_matches_interactive_selection(
    monkeypatch, tmp_path: Path,
) -> None:
    # AC 1 / AC 9a: the scoped picker is headed "Show which change?" and returns
    # the selected id. Display rows are built from _row_for_id over the raw ids.
    monkeypatch.setattr(m, "_interactive", lambda: True)
    monkeypatch.setattr(m, "_row_for_id", lambda c, cid: _row(cid))

    async def fake_picker(candidates, *, now, color, header, selected_index):
        assert "Show which change?" in header
        return candidates[1].change_id

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
