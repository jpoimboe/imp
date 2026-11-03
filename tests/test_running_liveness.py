"""The display word is derived from daemon-resolved liveness plus the durable
`halt_condition`: a live quest ⇒ `running` (or `waiting` at a gate), a not-live
quest ⇒ the at-rest status (`error` when the last halt was a failure, else the
stored word) — never `errored`. There is no `draft` display word: an
un-materialized new/paused change shows its real status (`new`/`paused`)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import imp.cli.main as m
import imp.cli.store as s
from imp.cli.store import HomeDigest
from imp.engine.quest import Quest
from imp.engine.state import State
from tests.conftest import _seed_control


def _dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_resolver_live_working_is_running() -> None:
    assert s.resolve_display_status("paused", lock_live=True, halt_condition=None) == "running"


def test_resolver_live_gate_is_waiting() -> None:
    assert s.resolve_display_status("paused", lock_live=True, halt_condition="waiting") == "waiting"


def test_resolver_not_live_error_is_error() -> None:
    assert s.resolve_display_status("error", lock_live=False, halt_condition=None) == "error"


def test_resolver_not_live_clean_is_at_rest_status() -> None:
    assert s.resolve_display_status("paused", lock_live=False, halt_condition=None) == "paused"


def test_resolver_absent_lock_keeps_at_rest_status() -> None:
    assert s.resolve_display_status("new", lock_live=False, halt_condition=None) == "new"


def test_resolver_completed_passes_through() -> None:
    status = s.resolve_display_status("completed", lock_live=False, halt_condition=None)
    assert status == "completed"


def test_membership_gates_reflect_derived_words() -> None:
    assert "waiting" in s._INFLIGHT and "error" in s._INFLIGHT
    assert "draft" not in s._INFLIGHT
    assert "error" in m.RESUMABLE          # a failed quest stays resumable
    assert "waiting" not in m.RESUMABLE    # live-only, never a resume target
    assert "draft" not in m.RESUMABLE
    assert "archived" in m.RESUMABLE        # an archived conversation rehydrates and resumes


def test_resumable_and_inflight_include_new_not_errored() -> None:
    assert "new" in m.RESUMABLE and "errored" not in m.RESUMABLE
    assert "new" in s._INFLIGHT and "errored" not in s._INFLIGHT


def test_status_rows_surfaces_crashed_run_as_at_rest_not_errored(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Crashed run",
                       quest_id="2026-06-11-deadaa", base_branch="main")
    # Simulate a crash: a leftover lock file naming a dead holder pid.
    (ch.quest_dir / "lock").write_text(str(_dead_pid()))
    rows = {r[0]: r for r in s._status_rows(tmp_path)}
    assert rows[ch.id][1] == "new"                       # at-rest, never errored
    assert ch.id in {r[0] for r in m._resumable_rows(tmp_path)}


def test_status_rows_live_gate_shows_waiting(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Gated", quest_id="gate01", base_branch="main")
    st = State.load(ch.state_dir)
    st.halt_condition = "waiting"
    st.save(ch.state_dir)
    (ch.quest_dir / "lock").write_text(str(os.getpid()))   # live holder pid
    rows = {r[0]: r for r in s._status_rows(tmp_path)}
    assert rows["gate01"][1] == "waiting"


def test_status_rows_not_live_error_is_error_and_resumable(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Broke", quest_id="err01", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "error"
    st.save(ch.state_dir)
    rows = {r[0]: r for r in s._status_rows(tmp_path)}
    assert rows["err01"][1] == "error"
    assert "err01" in {r[0] for r in m._resumable_rows(tmp_path)}


def _digest(quest_id: str, status: str, *, lock_live: bool) -> HomeDigest:
    meta = {"title": "Masked", "created_at": "2026-06-11T00:00:00+00:00",
            "materialized": True}
    return HomeDigest(
        quest_id=quest_id,
        quest_json=json.dumps({**meta, "id": quest_id}).encode(),
        state_json=json.dumps({"status": status, "step": "plan"}).encode(),
        meta=meta,
        last_iso="2026-06-11T11:00:00+00:00",
        has_user_message=True,
        prose="",
        intent_title=None,
        lock_live=lock_live,
    )


def test_status_rows_uses_daemon_liveness_when_lock_absent(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    d = _digest("masked1", "paused", lock_live=True)   # no lock file on disk
    rows = {r[0]: r for r in s._status_rows(tmp_path, scanned=[d])}
    assert rows["masked1"][1] == "running"             # digest bit wins over masked lock


def test_status_rows_lock_live_false_preserves_stored_status(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    d = _digest("masked2", "paused", lock_live=False)
    rows = {r[0]: r for r in s._status_rows(tmp_path, scanned=[d])}
    assert rows["masked2"][1] == "paused"              # not live -> stored word passes through


def test_lock_live_backlog_seed_promoted_to_active(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    meta = {"title": "Seed", "created_at": "2026-06-11T00:00:00+00:00",
            "materialized": False, "origin": "backlog"}
    d = HomeDigest(
        quest_id="seed1",
        quest_json=json.dumps({**meta, "id": "seed1"}).encode(),
        state_json=json.dumps({"status": "new", "step": "plan"}).encode(),
        meta=meta,
        last_iso="2026-06-11T11:00:00+00:00",
        has_user_message=False,
        prose="",
        intent_title=None,
        lock_live=True,                                # a live sibling seed
    )
    active = {r[0]: r for r in s._status_rows(tmp_path, scanned=[d])}
    backlog = {r[0] for r in s._backlog_rows(tmp_path, scanned=[d])}
    assert active["seed1"][1] == "running"             # promoted out of Backlog into Active
    assert "seed1" not in backlog                      # no longer classified as a backlog seed
