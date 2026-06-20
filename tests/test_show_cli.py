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
from imp.engine.state import State
from tests.conftest import _seed_control

_ID = "feat01"


def _seed_with_artifacts(control: Path, change_id: str, title: str) -> Change:
    ch = Change.create(control, title=title, change_id=change_id, base_branch="main")
    spec = ch.change_dir / "artifacts" / "spec"
    spec.mkdir(parents=True)
    (spec / f"{change_id}.md").write_text("# SPEC BODY\n")
    plans = ch.change_dir / "artifacts" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{change_id}-plan-1.md").write_text("# PLAN ONE\n")
    (plans / f"{change_id}-plan-2.md").write_text("# PLAN TWO\n")
    return ch


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
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    # Append a review-phase step event and a non-review one.
    with ch.events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "step_started", "agent_id": None,
                             "payload": {"step": "plan-reviewer", "phase": "plan"}}) + "\n")
        fh.write(json.dumps({"type": "step_started", "agent_id": None,
                             "payload": {"step": "task-lead", "phase": "implement"}}) + "\n")
    # events: the raw line is present.
    assert "plan-reviewer" in _run(monkeypatch, tmp_path, "show", _ID, "events")
    # review: only review-phase steps are listed.
    review_out = _run(monkeypatch, tmp_path, "show", _ID, "review")
    assert "plan-reviewer" in review_out
    assert "task-lead" not in review_out


def test_show_spec_falls_back_to_journal_after_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    files = {str(p.relative_to(ch.change_dir / "artifacts")): p
             for p in sorted((ch.change_dir / "artifacts").rglob("*")) if p.is_file()}
    worktree.artifact_commit(tmp_path, _ID, files, message="artifact: spec")
    st = State.load(ch.state_dir)
    st.status = "completed"
    st.save(ch.state_dir)
    with ch.events_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "step_started", "agent_id": None,
                             "payload": {"step": "plan-reviewer", "phase": "plan"}}) + "\n")
    worktree.fold_wip_into_journal(tmp_path, _ID, {
        "events.jsonl": ch.events_path,
        "change.json": ch.change_dir / "change.json",
        "state.json": ch.state_dir / "state.json",
    })
    worktree.remove_worktree(tmp_path, _ID)
    assert not (tmp_path / ".imp" / "worktrees" / _ID).exists()
    # Worktree gone -> journal fallback.
    assert "SPEC BODY" in _run(monkeypatch, tmp_path, "show", _ID, "spec")
    assert "PLAN TWO" in _run(monkeypatch, tmp_path, "show", _ID, "plan")
    assert "completed" in _run(monkeypatch, tmp_path, "show", _ID, "status")
    assert "plan-reviewer" in _run(monkeypatch, tmp_path, "show", _ID, "events")


def test_show_missing_change_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as m
    _seed_control(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    rc = main(["show", "nonexistent-change-id", "spec"])
    assert rc == 2


def test_show_backlog_default_renders_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.engine import backlog
    _seed_control(tmp_path)
    backlog.write_record(
        tmp_path, change_id="2026-06-16-bkg111", title="Add OAuth",
        description="wire up OAuth login", depends_on=[], created_by="human")
    out = _run(monkeypatch, tmp_path, "show", "2026-06-16-bkg111")
    assert "Add OAuth" in out
    assert "backlog" in out
    assert "ready" in out
    assert "wire up OAuth login" in out


def test_show_backlog_status_prints_bare_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.engine import backlog
    _seed_control(tmp_path)
    backlog.write_record(
        tmp_path, change_id="2026-06-16-bkg111", title="Add OAuth",
        description="d", depends_on=[], created_by="human")
    out = _run(monkeypatch, tmp_path, "show", "2026-06-16-bkg111", "status")
    assert out.strip() == "backlog"


def test_show_backlog_spec_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as m
    from imp.engine import backlog
    _seed_control(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    backlog.write_record(
        tmp_path, change_id="2026-06-16-bkg111", title="Add OAuth",
        description="d", depends_on=[], created_by="human")
    rc = main(["show", "2026-06-16-bkg111", "spec"])
    assert rc == 2


def test_backlog_card_omits_depends_on_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.cli.main import _backlog_card
    from datetime import datetime, UTC
    _seed_control(tmp_path)
    rec = {
        "id": "bk001", "title": "Do X", "description": "",
        "depends_on": [], "created_at": None,
    }
    lines = _backlog_card(rec, tmp_path, now=datetime.now(UTC))
    joined = "\n".join(lines)
    assert "depends_on" not in joined


def test_backlog_card_omits_description_section_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.cli.main import _backlog_card
    from datetime import datetime, UTC
    _seed_control(tmp_path)
    rec = {
        "id": "bk001", "title": "Do X", "description": "",
        "depends_on": [], "created_at": None,
    }
    lines = _backlog_card(rec, tmp_path, now=datetime.now(UTC))
    # no blank separator line and no description text
    assert "" not in lines


def test_backlog_card_includes_depends_on_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.cli.main import _backlog_card
    from datetime import datetime, UTC
    _seed_control(tmp_path)
    rec = {
        "id": "bk001", "title": "Do X", "description": "",
        "depends_on": ["dep001"], "created_at": None,
    }
    lines = _backlog_card(rec, tmp_path, now=datetime.now(UTC))
    assert any("depends_on" in line for line in lines)
    assert any("dep001" in line for line in lines)


def test_show_live_default_renders_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    _seed_with_artifacts(tmp_path, _ID, "Feat")
    out = _run(monkeypatch, tmp_path, "show", _ID)
    assert "Feat" in out
    assert "running" in out


def test_show_journaled_default_renders_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    files = {str(p.relative_to(ch.change_dir / "artifacts")): p
             for p in sorted((ch.change_dir / "artifacts").rglob("*")) if p.is_file()}
    worktree.artifact_commit(tmp_path, _ID, files, message="artifact: spec")
    st = State.load(ch.state_dir)
    st.status = "completed"
    st.save(ch.state_dir)
    worktree.fold_wip_into_journal(tmp_path, _ID, {
        "events.jsonl": ch.events_path,
        "change.json": ch.change_dir / "change.json",
        "state.json": ch.state_dir / "state.json",
    })
    worktree.remove_worktree(tmp_path, _ID)
    out = _run(monkeypatch, tmp_path, "show", _ID)
    assert "Feat" in out
    assert "completed" in out
