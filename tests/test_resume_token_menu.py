from __future__ import annotations

import json
from pathlib import Path

import pytest

import imp.cli.main as m
from imp.engine.abandon import archive_conversation
from imp.engine.archive import archive_quest
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


def _archive_terminal(control: Path, cid: str, status: str) -> None:
    events = control / f"{cid}.jsonl"
    events.write_text("")
    cj = control / f"{cid}.quest.json"
    cj.write_text(json.dumps({
        "id": cid, "title": cid.title(), "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    sj = control / f"{cid}.state.json"
    sj.write_text(json.dumps({"id": cid, "status": status, "step": "merge"}))
    archive_quest(control, cid, events_path=events,
                   quest_json_path=cj, state_json_path=sj)


# --- branch selection, isolated via monkeypatched _resumable_match_rows -------

@pytest.mark.anyio
async def test_blank_token_delegates_to_full_picker(monkeypatch, tmp_path: Path) -> None:
    # AC 13: blank/whitespace token behaves like no token (full resumable picker).
    async def fake_full(control, *, non_interactive):
        return "FULL"

    def boom_match(*a, **k):
        raise AssertionError("match rows consulted for a blank token")

    monkeypatch.setattr(m, "_pick_resume_target", fake_full)
    monkeypatch.setattr(m, "_resumable_match_rows", boom_match)
    assert await m._resolve_resume_target(
        tmp_path, None, non_interactive=True) == "FULL"
    assert await m._resolve_resume_target(
        tmp_path, "   ", non_interactive=True) == "FULL"


@pytest.mark.anyio
async def test_single_match_resolves_without_resolve_id(
    monkeypatch, tmp_path: Path,
) -> None:
    # AC 7: exactly one resumable match resolves directly, no menu, no fall-through.
    monkeypatch.setattr(m, "_resumable_match_rows", lambda c, t: [_row("lodo40")])

    def boom(*a, **k):
        raise AssertionError("_resolve_id invoked for a single match")

    monkeypatch.setattr(m, "_resolve_id", boom)
    assert await m._resolve_resume_target(
        tmp_path, "lodo", non_interactive=True) == "lodo40"


@pytest.mark.anyio
async def test_two_matches_open_scoped_picker_interactive(
    monkeypatch, tmp_path: Path,
) -> None:
    # AC 8: two-or-more matches open the scoped picker; selection is returned.
    monkeypatch.setattr(m, "_resumable_match_rows",
                        lambda c, t: [_row("lodo40"), _row("lodo77")])
    monkeypatch.setattr(m, "_interactive", lambda: True)

    async def fake_picker(candidates, *, now, color, header, selected_index):
        assert "Resume which change?" in header
        return candidates[1].quest_id

    monkeypatch.setattr(m.resume_select, "run_picker", fake_picker)
    assert await m._resolve_resume_target(
        tmp_path, "lodo", non_interactive=False) == "lodo77"


@pytest.mark.anyio
async def test_two_matches_non_tty_errors_pass_token(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    # AC 14: non-interactive terminal with two-or-more matches errors (no menu),
    # directing the user to pass a token.
    monkeypatch.setattr(m, "_resumable_match_rows",
                        lambda c, t: [_row("lodo40"), _row("lodo77")])
    monkeypatch.setattr(m, "_interactive", lambda: False)

    async def boom(*a, **k):
        raise AssertionError("picker drawn on a non-tty")

    monkeypatch.setattr(m.resume_select, "run_picker", boom)
    rc = await m._resolve_resume_target(tmp_path, "lodo", non_interactive=False)
    err = capsys.readouterr().err
    assert rc == 2
    assert "lodo40" in err and "lodo77" in err   # candidate listing
    assert "pass a token" in err


@pytest.mark.anyio
async def test_zero_matches_fall_through_to_resolve_id(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(m, "_resumable_match_rows", lambda c, t: [])
    monkeypatch.setattr(m, "_resolve_id", lambda c, t: "arch99")
    assert await m._resolve_resume_target(
        tmp_path, "arch", non_interactive=True) == "arch99"


@pytest.mark.anyio
async def test_zero_matches_resolve_id_systemexit_returns_2(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(m, "_resumable_match_rows", lambda c, t: [])

    def raise_exit(c, t):
        raise SystemExit(2)

    monkeypatch.setattr(m, "_resolve_id", raise_exit)
    assert await m._resolve_resume_target(
        tmp_path, "zzz", non_interactive=True) == 2


# --- end-to-end over real control roots (no monkeypatching) -------------------

@pytest.mark.anyio
async def test_real_single_live_resumable_resolves_directly(tmp_path: Path) -> None:
    # AC 7
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="A", quest_id="lodo40",
                        base_branch="main", origin="idea")
    assert await m._resolve_resume_target(
        tmp_path, "lodo", non_interactive=True) == "lodo40"


@pytest.mark.anyio
async def test_real_archived_resumable_resolves_by_stem_and_full_id(
    tmp_path: Path,
) -> None:
    # AC 9: archived-but-resumable reachable by both a shorter unique stem and a
    # full-id token.
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="Left", quest_id="lodo40",
                             base_branch="main")
    archive_conversation(ch)
    assert await m._resolve_resume_target(
        tmp_path, "lodo", non_interactive=True) == "lodo40"
    assert await m._resolve_resume_target(
        tmp_path, "lodo40", non_interactive=True) == "lodo40"


@pytest.mark.anyio
async def test_real_lone_archived_terminal_falls_through_to_resume_gone(
    tmp_path: Path, capsys,
) -> None:
    # AC 11: a lone archived completed change is not a resumable match; it falls
    # through to _resolve_id, and _load_or_rehydrate then prints the friendly
    # "already completed" message (rc 0), not a flat no-match error.
    _seed_control(tmp_path)
    _archive_terminal(tmp_path, "done40", "completed")
    target = await m._resolve_resume_target(
        tmp_path, "done40", non_interactive=True)
    assert target == "done40"
    assert m._load_or_rehydrate(tmp_path, "done40") == 0
    assert "already completed" in capsys.readouterr().out


@pytest.mark.anyio
async def test_real_unknown_token_returns_2(tmp_path: Path, capsys) -> None:
    # AC 12
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="A", quest_id="lodo40",
                        base_branch="main", origin="idea")
    rc = await m._resolve_resume_target(
        tmp_path, "zzzzzz", non_interactive=True)
    assert rc == 2
    assert "no Change matches" in capsys.readouterr().err


@pytest.mark.anyio
async def test_real_blank_token_behaves_like_no_token(tmp_path: Path) -> None:
    # AC 13: "" resolves identically to None (both the full picker; a lone
    # resumable auto-selects on a non-tty).
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="A", quest_id="lodo40",
                        base_branch="main", origin="idea")
    none_res = await m._resolve_resume_target(
        tmp_path, None, non_interactive=True)
    blank_res = await m._resolve_resume_target(
        tmp_path, "   ", non_interactive=True)
    assert none_res == blank_res == "lodo40"


def test_resolve_id_still_reaches_archive(tmp_path: Path) -> None:
    # AC 16: log/show/search/edit resolve through _resolve_id, which still spans
    # the archive. This plan does not touch _resolve_id.
    _seed_control(tmp_path)
    _archive_terminal(tmp_path, "arch40", "completed")
    assert m._resolve_id(tmp_path, "arch40") == "arch40"
