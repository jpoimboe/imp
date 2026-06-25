from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import get_args

from imp.engine.state import State, Status, lock_is_live


def _dead_pid() -> int:
    # A reaped child's pid is gone (and not reused within this test).
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_persisted_status_set_is_at_rest_only() -> None:
    assert set(get_args(Status)) == {
        "new", "paused", "merge_blocked", "completed", "saved", "abandoned"
    }


def _write_state(state_dir: Path, status: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(json.dumps(
        {"id": "c", "step": "s", "step_attempt": 0, "status": status}))


def test_load_coerces_legacy_running_to_new(tmp_path: Path) -> None:
    _write_state(tmp_path, "running")
    assert State.load(tmp_path).status == "new"


def test_load_coerces_legacy_errored_to_paused(tmp_path: Path) -> None:
    _write_state(tmp_path, "errored")
    assert State.load(tmp_path).status == "paused"


def test_load_leaves_at_rest_status_untouched(tmp_path: Path) -> None:
    _write_state(tmp_path, "merge_blocked")
    assert State.load(tmp_path).status == "merge_blocked"


def test_lock_is_live_true_for_living_pid(tmp_path: Path) -> None:
    (tmp_path / "lock").write_text(str(os.getpid()))
    assert lock_is_live(tmp_path / "lock") is True


def test_lock_is_live_false_for_dead_pid(tmp_path: Path) -> None:
    (tmp_path / "lock").write_text(str(_dead_pid()))
    assert lock_is_live(tmp_path / "lock") is False


def test_lock_is_live_false_when_lock_absent(tmp_path: Path) -> None:
    assert lock_is_live(tmp_path / "lock") is False
