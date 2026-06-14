from __future__ import annotations

import json
from pathlib import Path

import imp.cli.main as m
from imp.engine.change import Change
from imp.engine.state import State


def _make_change(root: Path) -> Change:
    change_dir = root / ".imp" / "change"
    (change_dir / "state").mkdir(parents=True)
    (change_dir / "change.json").write_text(json.dumps(
        {"id": "c", "title": "t", "base_branch": "main"}, sort_keys=True))
    State(id="c", step="spec", step_attempt=0, status="running",
          vars={}, loop_counts={}).save(change_dir / "state")
    return Change(id="c", title="t", base_branch="main",
                  control_root=root, project_root=root, change_dir=change_dir)


def test_abandon_confirmation_reflects_dead_holder(
    tmp_path: Path, capsys, monkeypatch) -> None:
    change = _make_change(tmp_path)
    # A crashed run leaves status="running" plus a lock file naming a dead pid.
    # PID 2**31-1 is never a live process — the signal-0 probe maps it to dead.
    (change.change_dir / "lock").write_text(str(2**31 - 1))

    # _abandon resolves the Change via Change.load (worktree layout) and reads
    # the commit count via git — neither exists in this bare tmp_path, so stub
    # both: the test targets only the confirmation's status rendering.
    monkeypatch.setattr(m.Change, "load", lambda control, cid: change)
    monkeypatch.setattr(m.worktree, "commit_count", lambda *a, **k: 0)

    rc = m._abandon(tmp_path, "c", assume_yes=False, interactive=True,
                    input_fn=lambda _p: "n")
    out = capsys.readouterr().out
    assert rc == 1                      # cancelled (input_fn returned "n")
    assert "status running" not in out  # the stale liveness must not surface
    assert "status errored" in out      # re-derived against the dead lock pid
