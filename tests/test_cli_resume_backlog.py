from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from imp.cli import main
from imp.engine import backlog
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _resume_args(token: str) -> argparse.Namespace:
    return argparse.Namespace(token=token, non_interactive=True, message=None,
                              from_target=None, tui=False)


def test_title_for_id_returns_backlog_title(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    backlog.write_record(tmp_path, change_id="2026-06-16-bkg444", title="My Title",
                         description="b", depends_on=[], created_by="human")
    title = main._title_for_id(tmp_path, "2026-06-16-bkg444")
    assert title == "My Title"


def test_iter_change_ids_includes_backlog(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    backlog.write_record(tmp_path, change_id="2026-06-16-bkg111", title="Iter test",
                         description="b", depends_on=[], created_by="human")
    assert "2026-06-16-bkg111" in main._iter_change_ids(tmp_path)


@pytest.mark.anyio
async def test_resume_backlog_materializes_and_runs(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    backlog.write_record(tmp_path, change_id="2026-06-16-bkg222", title="Add OAuth",
                         description="wire OAuth login", depends_on=[], created_by="human")

    captured: dict = {}

    async def _fake_run(change, *, args, seed_opening):
        captured["change"] = change
        captured["seed_opening"] = seed_opening
        return 0

    monkeypatch.setattr(main, "_run_fresh_session", _fake_run)
    rc = await main._maybe_resume_backlog(
        _resume_args("2026-06-16-bkg222"), tmp_path, "2026-06-16-bkg222")

    assert rc == 0
    assert captured["seed_opening"] == "Add OAuth\n\nwire OAuth login"
    assert captured["change"].id == "2026-06-16-bkg222"
    # Materialized: worktree exists, record removed.
    assert (tmp_path / ".imp" / "worktrees" / "2026-06-16-bkg222").is_dir()
    assert backlog.read_record(tmp_path, "2026-06-16-bkg222") is None


@pytest.mark.anyio
async def test_resume_backlog_warns_on_blocked(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    backlog.write_record(tmp_path, change_id="2026-06-16-bkg333", title="X",
                         description="b", depends_on=["2026-06-16-missing"],
                         created_by="human")
    async def _fake_run(change, *, args, seed_opening):
        return 0

    monkeypatch.setattr(main, "_run_fresh_session", _fake_run)
    await main._maybe_resume_backlog(
        _resume_args("2026-06-16-bkg333"), tmp_path, "2026-06-16-bkg333")
    err = capsys.readouterr().err
    assert "2026-06-16-missing" in err
    assert "blocked" in err.lower()


@pytest.mark.anyio
async def test_maybe_resume_backlog_returns_none_for_non_backlog(
        tmp_path: Path) -> None:
    _seed_control(tmp_path)
    rc = await main._maybe_resume_backlog(
        _resume_args("2026-06-16-none00"), tmp_path, "2026-06-16-none00")
    assert rc is None
