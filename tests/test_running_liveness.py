"""The display word is derived from the lock plus the durable `halt_condition`:
a live lock ⇒ `running` (or `waiting` at a gate), a stale lock (dead pid) or
absent lock ⇒ the at-rest status (`error` when the last halt was a failure, else
the stored word) — never `errored`. There is no `draft` display word: an
un-materialized new/paused change shows its real status (`new`/`paused`)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import imp.cli.main as m
from imp.engine.quest import Quest
from imp.engine.state import State
from tests.conftest import _seed_control


def _dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_resolver_live_working_is_running(tmp_path: Path) -> None:
    (tmp_path / "lock").write_text(str(os.getpid()))
    assert m.resolve_display_status("paused", tmp_path, halt_condition=None) == "running"


def test_resolver_live_gate_is_waiting(tmp_path: Path) -> None:
    (tmp_path / "lock").write_text(str(os.getpid()))
    assert m.resolve_display_status("paused", tmp_path, halt_condition="waiting") == "waiting"


def test_resolver_not_live_error_is_error(tmp_path: Path) -> None:
    (tmp_path / "lock").write_text(str(_dead_pid()))
    assert m.resolve_display_status("paused", tmp_path, halt_condition="error") == "error"


def test_resolver_not_live_clean_is_at_rest_status(tmp_path: Path) -> None:
    (tmp_path / "lock").write_text(str(_dead_pid()))
    assert m.resolve_display_status("paused", tmp_path, halt_condition=None) == "paused"


def test_resolver_absent_lock_keeps_at_rest_status(tmp_path: Path) -> None:
    assert m.resolve_display_status("new", tmp_path, halt_condition=None) == "new"


def test_resolver_completed_folds_to_merged(tmp_path: Path) -> None:
    assert m.resolve_display_status("completed", tmp_path, halt_condition=None) == "merged"


def test_membership_gates_reflect_derived_words() -> None:
    assert "waiting" in m._INFLIGHT and "error" in m._INFLIGHT
    assert "draft" not in m._INFLIGHT
    assert "error" in m.RESUMABLE          # a failed quest stays resumable
    assert "waiting" not in m.RESUMABLE    # live-only, never a resume target
    assert "draft" not in m.RESUMABLE


def test_resumable_and_inflight_include_new_not_errored() -> None:
    assert "new" in m.RESUMABLE and "errored" not in m.RESUMABLE
    assert "new" in m._INFLIGHT and "errored" not in m._INFLIGHT


def test_status_rows_surfaces_crashed_run_as_at_rest_not_errored(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Crashed run",
                       quest_id="2026-06-11-deadaa", base_branch="main")
    # Simulate a crash: a leftover lock file naming a dead holder pid.
    (ch.quest_dir / "lock").write_text(str(_dead_pid()))
    rows = {r[0]: r for r in m._status_rows(tmp_path)}
    assert rows[ch.id][1] == "new"                       # at-rest, never errored
    assert ch.id in {r[0] for r in m._resumable_rows(tmp_path)}


def test_status_rows_live_gate_shows_waiting(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Gated", quest_id="gate01", base_branch="main")
    st = State.load(ch.state_dir)
    st.halt_condition = "waiting"
    st.save(ch.state_dir)
    (ch.quest_dir / "lock").write_text(str(os.getpid()))   # live holder pid
    rows = {r[0]: r for r in m._status_rows(tmp_path)}
    assert rows["gate01"][1] == "waiting"


def test_status_rows_not_live_error_is_error_and_resumable(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Broke", quest_id="err01", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.halt_condition = "error"
    st.save(ch.state_dir)
    rows = {r[0]: r for r in m._status_rows(tmp_path)}
    assert rows["err01"][1] == "error"
    assert "err01" in {r[0] for r in m._resumable_rows(tmp_path)}
