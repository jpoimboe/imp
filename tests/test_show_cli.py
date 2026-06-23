"""imp show reads live artifacts, then the archive after merge."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from imp.cli.main import main
from imp.engine import archive, worktree
from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _seed_control

_ID = "feat01"


def _git_wip_present(control: Path, change_id: str) -> bool:
    import subprocess

    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/imp/journal-wip/{change_id}"],
            cwd=control,
            capture_output=True,
        ).returncode
        == 0
    )


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    _seed_with_artifacts(tmp_path, _ID, "Feat")
    out = _run(monkeypatch, tmp_path, "show", _ID, "plan")
    assert "PLAN TWO" in out  # newest plan
    assert "PLAN ONE" not in out


def test_show_status_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    _seed_with_artifacts(tmp_path, _ID, "Feat")
    assert "running" in _run(monkeypatch, tmp_path, "show", _ID, "status")


def test_show_events_and_review_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    # Append a review-phase step event and a non-review one.
    with ch.events_path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "step_started",
                    "agent_id": None,
                    "payload": {"step": "plan-reviewer", "phase": "plan"},
                }
            )
            + "\n"
        )
        fh.write(
            json.dumps(
                {
                    "type": "step_started",
                    "agent_id": None,
                    "payload": {"step": "task-lead", "phase": "implement"},
                }
            )
            + "\n"
        )
    # events: the raw line is present.
    assert "plan-reviewer" in _run(monkeypatch, tmp_path, "show", _ID, "events")
    # review: only review-phase steps are listed.
    review_out = _run(monkeypatch, tmp_path, "show", _ID, "review")
    assert "plan-reviewer" in review_out
    assert "task-lead" not in review_out


def test_show_spec_falls_back_after_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    files = {
        str(p.relative_to(ch.change_dir / "artifacts")): p
        for p in sorted((ch.change_dir / "artifacts").rglob("*"))
        if p.is_file()
    }
    worktree.artifact_commit(tmp_path, _ID, files, message="artifact: spec")
    st = State.load(ch.state_dir)
    st.status = "completed"
    st.save(ch.state_dir)
    with ch.events_path.open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "step_started",
                    "agent_id": None,
                    "payload": {"step": "plan-reviewer", "phase": "plan"},
                }
            )
            + "\n"
        )
    from imp.engine import archive

    archive.archive_change(
        tmp_path,
        _ID,
        events_path=ch.events_path,
        change_json_path=ch.change_dir / "change.json",
        state_json_path=ch.state_dir / "state.json",
    )
    # The per-Change artifact ref is RETAINED at archive (spec AC 3).
    assert _git_wip_present(tmp_path, _ID)
    worktree.remove_worktree(tmp_path, _ID)
    assert not (tmp_path / ".imp" / "worktrees" / _ID).exists()
    # Worktree gone -> archive/journal fallback.
    assert "SPEC BODY" in _run(monkeypatch, tmp_path, "show", _ID, "spec")
    assert "PLAN TWO" in _run(monkeypatch, tmp_path, "show", _ID, "plan")
    assert "completed" in _run(monkeypatch, tmp_path, "show", _ID, "status")
    assert "plan-reviewer" in _run(monkeypatch, tmp_path, "show", _ID, "events")


def test_show_missing_change_returns_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    rc = main(["show", "nonexistent-change-id", "spec"])
    assert rc == 2


def test_show_backlog_default_renders_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    Change.create_draft(
        tmp_path,
        title="Add OAuth",
        change_id="2026-06-16-bkg111",
        base_branch="main",
        origin="followup",
        seed_opening="Add OAuth\n\nwire up OAuth login",
    )
    out = _run(monkeypatch, tmp_path, "show", "2026-06-16-bkg111")
    assert "Add OAuth" in out
    assert "backlog" in out
    assert "ready" in out
    assert "wire up OAuth login" in out


def test_show_backlog_status_prints_bare_word(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    Change.create_draft(
        tmp_path,
        title="Add OAuth",
        change_id="2026-06-16-bkg111",
        base_branch="main",
        origin="idea",
        seed_opening="Add OAuth",
    )
    out = _run(monkeypatch, tmp_path, "show", "2026-06-16-bkg111", "status")
    assert out.strip() == "backlog"


def test_show_backlog_spec_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    Change.create_draft(
        tmp_path,
        title="Add OAuth",
        change_id="2026-06-16-bkg111",
        base_branch="main",
        origin="idea",
        seed_opening="Add OAuth",
    )
    rc = main(["show", "2026-06-16-bkg111", "spec"])
    assert rc == 2


def test_backlog_card_omits_depends_on_when_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from imp.cli.main import _backlog_card

    _seed_control(tmp_path)
    meta = {
        "id": "bk001",
        "title": "Do X",
        "origin": "idea",
        "depends_on": [],
        "created_at": None,
        "seed_opening": "Do X",
    }
    joined = "\n".join(_backlog_card(meta, tmp_path, now=datetime.now(UTC)))
    assert "depends_on" not in joined
    assert "provenance: idea" in joined  # new meta-sourced field
    assert "status:     backlog" in joined
    assert joined.count("Do X") == 1  # title shown once, not duplicated in the body


def test_backlog_card_omits_body_section_when_seed_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from imp.cli.main import _backlog_card

    _seed_control(tmp_path)
    meta = {
        "id": "bk001",
        "title": "Do X",
        "origin": "idea",
        "depends_on": [],
        "created_at": None,
        "seed_opening": "",
    }
    lines = _backlog_card(meta, tmp_path, now=datetime.now(UTC))
    # no blank separator line and no body text
    assert "" not in lines


def test_backlog_card_includes_depends_on_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from imp.cli.main import _backlog_card

    _seed_control(tmp_path)
    meta = {
        "id": "bk001",
        "title": "Do X",
        "origin": "followup",
        "depends_on": ["dep001"],
        "created_at": None,
        "seed_opening": "Do X",
    }
    lines = _backlog_card(meta, tmp_path, now=datetime.now(UTC))
    assert any("depends_on" in line for line in lines)
    assert any("dep001" in line for line in lines)
    assert any(line == "provenance: followup" for line in lines)  # new meta-sourced field


def test_show_live_default_renders_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    _seed_with_artifacts(tmp_path, _ID, "Feat")
    out = _run(monkeypatch, tmp_path, "show", _ID)
    assert "Feat" in out
    assert "running" in out


def test_show_step_line_prints_raw_step_name_not_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Feat", change_id="shwstp", base_branch="main")
    st = State.load(ch.state_dir)
    st.step = "write_spec"
    st.step_label = "writing spec"  # label differs from the raw name
    st.save(ch.state_dir)
    out = _run(monkeypatch, tmp_path, "show", "shwstp")
    assert "write_spec" in out  # imp show prints the RAW step name
    assert "writing spec" not in out  # never the label


def test_show_spec_and_plan_from_wip_ref_when_home_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No change home: artifacts must come from the retained wip ref (spec AC 10, 12).
    _seed_control(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("# WIP-ONLY SPEC\n")
    p1 = tmp_path / "p1.md"
    p1.write_text("# PLAN A\n")
    p2 = tmp_path / "p2.md"
    p2.write_text("# PLAN B\n")
    worktree.artifact_commit(
        tmp_path,
        "ghost01",
        {"spec/ghost01.md": spec, "plans/ghost01-plan-1.md": p1, "plans/ghost01-plan-2.md": p2},
        message="artifact: ghost",
    )
    # Seed the archive runtime (events/change/state) so `_resolve_id` finds the
    # id while the artifacts remain reachable solely via the wip ref.
    events = tmp_path / "ghost-events.jsonl"
    events.write_text(
        '{"id":"ghost01","timestamp":"2026-06-01T00:00:00+00:00",'
        '"type":"change_created","payload":{}}\n'
    )
    cj = tmp_path / "ghost-change.json"
    cj.write_text(
        json.dumps(
            {
                "id": "ghost01",
                "title": "Ghost",
                "base_branch": "main",
                "created_at": "2026-06-01T00:00:00+00:00",
                "depends_on": [],
            }
        )
    )
    sj = tmp_path / "ghost-state.json"
    sj.write_text(json.dumps({"id": "ghost01", "status": "completed", "step": None}))
    archive.archive_change(
        tmp_path, "ghost01", events_path=events, change_json_path=cj, state_json_path=sj
    )

    assert "WIP-ONLY SPEC" in _run(monkeypatch, tmp_path, "show", "ghost01", "spec")
    out = _run(monkeypatch, tmp_path, "show", "ghost01", "plan")
    assert "PLAN B" in out  # newest
    assert "PLAN A" not in out


def test_show_abandoned_backlog_is_not_a_live_brief(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as m
    from imp.engine.abandon import tear_down_change

    _seed_control(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    change = Change.create_draft(
        tmp_path,
        title="Add OAuth",
        change_id="2026-06-16-bkg111",
        base_branch="main",
        origin="idea",
        seed_opening="Add OAuth",
    )
    tear_down_change(change)
    assert not (tmp_path / ".imp" / "changes" / "2026-06-16-bkg111").exists()
    out = _run(monkeypatch, tmp_path, "show", "2026-06-16-bkg111", "status")
    assert out.strip() != "backlog"  # History item, not a queued Backlog brief
    # log renders the archived conversation, not a brief
    assert main(["log", "2026-06-16-bkg111"]) == 0
