"""`running` is derived from the lock, not persisted: a live lock ⇒ running, a
stale lock (dead pid) or absent lock ⇒ the resumable at-rest status, never
`errored`. An un-materialized new/paused change shows as `draft`."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import imp.cli.main as m
from imp.engine.change import Change
from tests.conftest import _seed_control


def _dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_resolver_live_lock_is_running(tmp_path: Path) -> None:
    (tmp_path / "lock").write_text(str(os.getpid()))
    assert m.resolve_display_status("new", tmp_path, materialized=True) == "running"


def test_resolver_absent_lock_keeps_at_rest_status(tmp_path: Path) -> None:
    assert m.resolve_display_status("new", tmp_path, materialized=True) == "new"


def test_resolver_stale_lock_keeps_at_rest_status_never_errored(tmp_path: Path) -> None:
    (tmp_path / "lock").write_text(str(_dead_pid()))
    assert m.resolve_display_status("paused", tmp_path, materialized=True) == "paused"


def test_resolver_unmaterialized_new_is_draft(tmp_path: Path) -> None:
    assert m.resolve_display_status("new", tmp_path, materialized=False) == "draft"


def test_resumable_and_inflight_include_new_not_errored() -> None:
    assert "new" in m.RESUMABLE and "errored" not in m.RESUMABLE
    assert "new" in m._INFLIGHT and "errored" not in m._INFLIGHT


def test_status_rows_surfaces_crashed_run_as_at_rest_not_errored(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Crashed run",
                       quest_id="2026-06-11-deadaa", base_branch="main")
    # Simulate a crash: a leftover lock file naming a dead holder pid.
    (ch.quest_dir / "lock").write_text(str(_dead_pid()))
    rows = {r[0]: r for r in m._status_rows(tmp_path)}
    assert rows[ch.id][1] == "new"                       # at-rest, never errored
    assert ch.id in {r[0] for r in m._resumable_rows(tmp_path)}


def test_created_draft_with_no_lock_is_draft_never_running(tmp_path: Path) -> None:
    # Regression (m6rmch): a created draft with no lock must never show running.
    _seed_control(tmp_path)
    Change.create_draft(tmp_path, title="D", quest_id="m6rmch", base_branch="main")
    row = m._row_for_id(tmp_path, "m6rmch")
    assert row is not None
    assert row[1] == "draft"
