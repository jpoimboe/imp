from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.remote.daemon import discover
from imp.remote.daemon.relay_supervisor import RelaySupervisor


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_list_changes_skips_a_change_whose_row_read_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One broken change home must not blind the whole directory — the daemon's
    scan loop calls this, so a raise here would take down the daemon."""
    ids = ["good1", "boom", "good2"]

    def _row(_control: Path, cid: str) -> Any:
        if cid == "boom":
            raise OSError("mid-write / corrupt home")
        return (cid, "running", "plan", "", "T", "2026-01-01", None)

    monkeypatch.setattr("imp.cli.main._iter_change_ids", lambda _c: ids)
    monkeypatch.setattr("imp.cli.main._row_for_id", _row)

    got = {info.id for info in discover.list_changes(tmp_path)}
    assert got == {"good1", "good2"}  # "boom" skipped, not fatal


def test_snapshot_returns_none_when_row_read_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_control: Path, _cid: str) -> Any:
        raise OSError("transient")

    monkeypatch.setattr("imp.cli.main._row_for_id", _boom)
    assert discover.snapshot(tmp_path, "x") is None


@pytest.mark.anyio
async def test_supervisor_poll_survives_a_scan_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient failure of resident_session_ids must not crash the supervisor;
    it logs once and keeps polling."""
    calls = {"n": 0}

    def _resident(_control: Path) -> set[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("scan blew up")
        return set()

    monkeypatch.setattr(
        "imp.remote.daemon.relay_supervisor.resident_session_ids", _resident)
    sup = RelaySupervisor(
        control_root=tmp_path, sink=lambda _e: None, events=("all",),
        title_for=lambda _c: "T", poll_gap=0.01)
    async with anyio.create_task_group() as tg:
        tg.start_soon(sup.run)
        # If the first (raising) poll crashed the task group, this wait would never
        # see further polls and the group would surface the error on exit.
        with anyio.fail_after(5):
            while calls["n"] < 3:
                await anyio.sleep(0.01)
        tg.cancel_scope.cancel()
    assert calls["n"] >= 3  # kept polling after the error
