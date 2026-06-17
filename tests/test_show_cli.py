"""imp show reads live artifacts, then falls back to the journal after merge."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from imp.cli.main import main
from imp.engine import worktree
from imp.engine.change import Change
from imp.engine.slug import derive_handle
from imp.engine.state import State
from tests.conftest import _seed_control

_ID = "feat01"


def _seed_with_artifacts(control: Path, change_id: str, title: str) -> tuple[Change, str]:
    ch = Change.create(control, title=title, change_id=change_id, base_branch="main")
    handle = derive_handle(ch.title, ch.id)       # matches _show_handle artifact naming
    spec = ch.change_dir / "artifacts" / "spec"
    spec.mkdir(parents=True)
    (spec / f"{handle}.md").write_text("# SPEC BODY\n")
    plans = ch.change_dir / "artifacts" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{handle}-plan-1.md").write_text("# PLAN ONE\n")
    (plans / f"{handle}-plan-2.md").write_text("# PLAN TWO\n")
    return ch, handle


def _run(monkeypatch: pytest.MonkeyPatch, control: Path, *argv: str) -> str:
    import imp.cli.main as m
    monkeypatch.setattr(m, "_control_root", lambda: control)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(list(argv))
    assert rc == 0, buf.getvalue()
    return buf.getvalue()


def test_show_spec_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    _seed_with_artifacts(tmp_path, _ID, "Feat")
    assert "SPEC BODY" in _run(monkeypatch, tmp_path, "show", _ID, "spec")


def test_show_plan_live_shows_newest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    _seed_with_artifacts(tmp_path, _ID, "Feat")
    out = _run(monkeypatch, tmp_path, "show", _ID, "plan")
    assert "PLAN TWO" in out          # newest plan
    assert "PLAN ONE" not in out


def test_show_status_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    _seed_with_artifacts(tmp_path, _ID, "Feat")
    assert "running" in _run(monkeypatch, tmp_path, "show", _ID, "status")


def test_show_events_and_review_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch, _slug = _seed_with_artifacts(tmp_path, _ID, "Feat")
    # Append a review-phase step event and a non-review one.
    with ch.events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "step_started", "agent_id": None,
                             "payload": {"step": "plan-review", "phase": "plan"}}) + "\n")
        fh.write(json.dumps({"type": "step_started", "agent_id": None,
                             "payload": {"step": "task-lead", "phase": "implement"}}) + "\n")
    # events: the raw line is present.
    assert "plan-review" in _run(monkeypatch, tmp_path, "show", _ID, "events")
    # review: only review-phase steps are listed.
    review_out = _run(monkeypatch, tmp_path, "show", _ID, "review")
    assert "plan-review" in review_out
    assert "task-lead" not in review_out


def test_show_spec_falls_back_to_journal_after_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch, _slug = _seed_with_artifacts(tmp_path, _ID, "Feat")
    files = {str(p.relative_to(ch.change_dir / "artifacts")): p
             for p in sorted((ch.change_dir / "artifacts").rglob("*")) if p.is_file()}
    worktree.artifact_commit(tmp_path, _ID, files, message="artifact: spec")
    st = State.load(ch.state_dir)
    st.status = "completed"
    st.save(ch.state_dir)
    with ch.events_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "step_started", "agent_id": None,
                             "payload": {"step": "plan-review", "phase": "plan"}}) + "\n")
    worktree.fold_wip_into_journal(tmp_path, _ID, {
        "events.jsonl": ch.events_path,
        "change.json": ch.change_dir / "change.json",
        "state.json": ch.state_dir / "state.json",
    })
    worktree.remove_worktree(tmp_path, _ID)
    assert not (tmp_path / ".imp" / "worktrees" / _ID).exists()
    # Worktree gone -> journal fallback (handle re-derived from the journaled title).
    assert "SPEC BODY" in _run(monkeypatch, tmp_path, "show", _ID, "spec")
    assert "PLAN TWO" in _run(monkeypatch, tmp_path, "show", _ID, "plan")
    assert "completed" in _run(monkeypatch, tmp_path, "show", _ID, "status")
    assert "plan-review" in _run(monkeypatch, tmp_path, "show", _ID, "events")


def test_show_missing_change_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as m
    _seed_control(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    rc = main(["show", "nonexistent-change-id", "spec"])
    assert rc == 2
