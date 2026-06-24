from __future__ import annotations

from pathlib import Path

import pytest

import imp.cli.main as m


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# _Row = (id, status, step, last_iso, title, created_at)
def _row(cid: str, status: str, last: str) -> tuple[str, str, str, str, str, str, str | None]:
    return (cid, status, "plan", last, "Title " + cid, "2026-06-01T00:00:00+00:00", None)


@pytest.mark.anyio
async def test_nothing_to_resume_prints_hint(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(m, "_resumable_rows", lambda c: [])
    monkeypatch.setattr(m, "_past_change_count", lambda c: 3)
    rc = await m._pick_resume_target(tmp_path, non_interactive=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Nothing to resume." in out
    assert "3 past changes" in out
    assert "imp list --all" in out


@pytest.mark.anyio
async def test_nothing_to_resume_no_hint_when_no_past(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    monkeypatch.setattr(m, "_resumable_rows", lambda c: [])
    monkeypatch.setattr(m, "_past_change_count", lambda c: 0)
    rc = await m._pick_resume_target(tmp_path, non_interactive=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Nothing to resume." in out
    assert "past change" not in out


@pytest.mark.anyio
async def test_single_resumable_returns_its_id(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        m, "_resumable_rows",
        lambda c: [_row("2026-06-11-yaitd7", "paused", "2026-06-11T00:00:00+00:00")])
    assert await m._pick_resume_target(
        tmp_path, non_interactive=False) == "2026-06-11-yaitd7"


@pytest.mark.anyio
async def test_multiple_non_interactive_lists_candidates_and_exits_2(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    rows = [_row("2026-06-11-aaa111", "paused", "2026-06-11T00:00:00+00:00"),
            _row("2026-06-10-bbb222", "errored", "2026-06-10T00:00:00+00:00")]
    monkeypatch.setattr(m, "_resumable_rows", lambda c: rows)
    monkeypatch.setattr(m, "_interactive", lambda: False)
    rc = await m._pick_resume_target(tmp_path, non_interactive=False)
    err = capsys.readouterr().err
    assert rc == 2
    assert "NAME" in err                          # shared header rendered
    assert "aaa111" in err and "bbb222" in err    # both candidate tokens listed


@pytest.mark.anyio
async def test_explicit_non_interactive_flag_skips_picker_even_on_a_tty(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    rows = [_row("2026-06-11-aaa111", "paused", "2026-06-11T00:00:00+00:00"),
            _row("2026-06-10-bbb222", "errored", "2026-06-10T00:00:00+00:00")]
    monkeypatch.setattr(m, "_resumable_rows", lambda c: rows)
    monkeypatch.setattr(m, "_interactive", lambda: True)   # tty present, but flag wins

    async def boom_picker(candidates, *, now, color, header, selected_index=0):
        raise AssertionError("picker drawn despite --non-interactive")

    monkeypatch.setattr(m.resume_select, "run_picker", boom_picker)
    rc = await m._pick_resume_target(tmp_path, non_interactive=True)
    err = capsys.readouterr().err
    assert rc == 2
    assert "aaa111" in err and "bbb222" in err    # candidate list, no picker


@pytest.mark.anyio
async def test_multiple_interactive_uses_picker_selection(
    monkeypatch, tmp_path: Path,
) -> None:
    rows = [_row("2026-06-11-aaa111", "paused", "2026-06-11T00:00:00+00:00"),
            _row("2026-06-10-bbb222", "errored", "2026-06-10T00:00:00+00:00")]
    monkeypatch.setattr(m, "_resumable_rows", lambda c: rows)
    monkeypatch.setattr(m, "_interactive", lambda: True)

    async def fake_picker(candidates, *, now, color, header, selected_index=0):
        return candidates[1].change_id

    monkeypatch.setattr(m.resume_select, "run_picker", fake_picker)
    assert await m._pick_resume_target(
        tmp_path, non_interactive=False) == "2026-06-10-bbb222"


@pytest.mark.anyio
async def test_multiple_interactive_cancel_returns_2(
    monkeypatch, tmp_path: Path,
) -> None:
    rows = [_row("2026-06-11-aaa111", "paused", "2026-06-11T00:00:00+00:00"),
            _row("2026-06-10-bbb222", "errored", "2026-06-10T00:00:00+00:00")]
    monkeypatch.setattr(m, "_resumable_rows", lambda c: rows)
    monkeypatch.setattr(m, "_interactive", lambda: True)

    async def fake_picker(candidates, *, now, color, header, selected_index=0):
        return None

    monkeypatch.setattr(m.resume_select, "run_picker", fake_picker)
    assert await m._pick_resume_target(tmp_path, non_interactive=False) == 2
