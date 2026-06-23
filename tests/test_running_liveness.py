"""Finding D: a `running` Change whose ProjectLock holder is dead has crashed —
re-derive it as `errored` so it's visible in `imp ls` and resumable."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import imp.cli.main as m
from imp.engine.change import Change
from tests.conftest import _seed_control


def _dead_pid() -> int:
    # A reaped child's pid is gone (and not reused within this test).
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_effective_status_live_lock_stays_running(tmp_path: Path) -> None:
    (tmp_path / "lock").write_text(str(os.getpid()))   # this process is alive
    assert m._effective_status("running", tmp_path) == "running"


def test_effective_status_dead_lock_becomes_errored(tmp_path: Path) -> None:
    (tmp_path / "lock").write_text(str(_dead_pid()))
    assert m._effective_status("running", tmp_path) == "errored"


def test_effective_status_missing_lock_stays_running(tmp_path: Path) -> None:
    # Startup window between Change.create and the run acquiring the lock: no lock
    # file yet — leave it running rather than false-flag a crash.
    assert m._effective_status("running", tmp_path) == "running"


def test_effective_status_non_running_is_authoritative(tmp_path: Path) -> None:
    (tmp_path / "lock").write_text(str(_dead_pid()))
    for status in ("paused", "errored", "completed", "merge_blocked", "abandoned"):
        assert m._effective_status(status, tmp_path) == status


def test_status_rows_surfaces_crashed_running_as_errored(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Crashed run",
                       change_id="2026-06-11-deadaa", base_branch="main")
    assert m._status_row(  # sanity: Change.create starts it running
        ch.id, (ch.state_dir / "state.json").read_bytes(),
        (ch.change_dir / "change.json").read_bytes(), "")[1] == "running"
    # Simulate a crash: a leftover lock file with a dead holder pid.
    (ch.change_dir / "lock").write_text(str(_dead_pid()))

    rows = m._status_rows(tmp_path)
    by_id = {r[0]: r for r in rows}
    assert by_id[ch.id][1] == "errored"                 # re-derived from the dead lock
    # ...and it is therefore resumable.
    assert ch.id in {r[0] for r in m._resumable_rows(tmp_path)}
