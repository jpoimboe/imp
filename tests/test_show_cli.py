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


def _git_wip_present(control: Path, quest_id: str) -> bool:
    import subprocess

    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/imp/artifacts/{quest_id}"],
            cwd=control,
            capture_output=True,
        ).returncode
        == 0
    )


def _seed_with_artifacts(control: Path, quest_id: str, title: str) -> Change:
    ch = Change.create(control, title=title, quest_id=quest_id, base_branch="main")
    spec = ch.quest_dir / "artifacts" / "spec"
    spec.mkdir(parents=True)
    (spec / f"{quest_id}.md").write_text("# SPEC BODY\n")
    plans = ch.quest_dir / "artifacts" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{quest_id}-plan-1.md").write_text("# PLAN ONE\n")
    (plans / f"{quest_id}-plan-2.md").write_text("# PLAN TWO\n")
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
    assert "new" in _run(monkeypatch, tmp_path, "show", _ID, "status")


def test_show_events_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    # Append step events so the events resource has lines to show.
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


def test_show_rejects_review_resource(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["show", _ID, "review"])
    assert "invalid choice: 'review'" in capsys.readouterr().err


def test_show_spec_falls_back_after_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    files = {
        str(p.relative_to(ch.quest_dir / "artifacts")): p
        for p in sorted((ch.quest_dir / "artifacts").rglob("*"))
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

    archive.archive_quest(
        tmp_path,
        _ID,
        events_path=ch.events_path,
        quest_json_path=ch.quest_dir / "change.json",
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
        quest_id="2026-06-16-bkg111",
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
        quest_id="2026-06-16-bkg111",
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
        quest_id="2026-06-16-bkg111",
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
    assert "new" in out


def test_show_step_line_prints_human_labels_not_raw_slugs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Feat", quest_id="shwstp", base_branch="main")
    st = State.load(ch.state_dir)
    st.step = "write_spec"
    st.step_label = "writing spec"  # label differs from the raw slug
    st.phase_label = "spec phase"   # card sources the phase from persisted state
    st.save(ch.state_dir)
    with ch.events_path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "step_started",
                    "agent_id": None,
                    "payload": {
                        "step": "write_spec",
                        "phase": "spec",
                        "phase_label": "spec phase",
                    },
                }
            )
            + "\n"
        )
    out = _run(monkeypatch, tmp_path, "show", "shwstp")
    assert "spec phase · writing spec" in out  # <phase label> · <step label>
    assert "write_spec" not in out  # never the raw step slug


def test_show_step_line_omits_phase_when_no_phase_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No step_started event carries a phase_label -> show the step label alone,
    # with no raw phase slug and no leading separator.
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Feat", quest_id="shwnop", base_branch="main")
    st = State.load(ch.state_dir)
    st.step = "write_spec"
    st.step_label = "writing spec"
    st.save(ch.state_dir)
    out = _run(monkeypatch, tmp_path, "show", "shwnop")
    step_line = next(ln for ln in out.splitlines() if ln.startswith("step:"))
    assert "·" not in step_line  # no phase segment and no leading separator
    assert step_line.endswith("writing spec")  # step label alone, nothing trailing


def test_show_step_line_omits_stale_phase_label_across_phase_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A prior labeled phase then a current UNLABELED phase (its step_started
    # carries no phase_label): the stale prior label must not bleed forward.
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Feat", quest_id="shwstl", base_branch="main")
    st = State.load(ch.state_dir)
    st.step = "verify"
    st.step_label = "verifying"
    st.save(ch.state_dir)
    with ch.events_path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "step_started",
                    "agent_id": None,
                    "payload": {
                        "step": "write_spec",
                        "phase": "spec",
                        "phase_label": "spec phase",
                    },
                }
            )
            + "\n"
        )
        fh.write(
            json.dumps(
                {
                    "type": "step_started",
                    "agent_id": None,
                    "payload": {"step": "verify", "phase": "verify"},
                }
            )
            + "\n"
        )
    out = _run(monkeypatch, tmp_path, "show", "shwstl")
    step_line = next(ln for ln in out.splitlines() if ln.startswith("step:"))
    assert "spec phase" not in step_line  # stale prior-phase label must not bleed
    assert "·" not in step_line  # current phase is unlabeled -> no phase segment
    assert step_line.endswith("verifying")  # step label alone


def test_show_step_line_truncates_to_terminal_width(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Feat", quest_id="shwtrn", base_branch="main")
    st = State.load(ch.state_dir)
    st.step = "write_spec"
    st.step_label = "writing spec " + "x" * 200  # far wider than the 80-col test terminal
    st.save(ch.state_dir)
    out = _run(monkeypatch, tmp_path, "show", "shwtrn")
    step_line = next(ln for ln in out.splitlines() if ln.startswith("step:"))
    assert len(step_line) <= 80  # clipped to the (test) 80-col terminal width
    assert step_line.endswith("…")


def test_show_status_stays_bare_word_not_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `imp show <id> status` is the parseable form: bare status word only.
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Feat", quest_id="shwsta", base_branch="main")
    st = State.load(ch.state_dir)
    st.step = "write_spec"
    st.step_label = "writing spec"
    st.save(ch.state_dir)
    out = _run(monkeypatch, tmp_path, "show", "shwsta", "status").strip()
    assert out == "new"
    assert "·" not in out
    assert "writing spec" not in out


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
    archive.archive_quest(
        tmp_path, "ghost01", events_path=events, quest_json_path=cj, state_json_path=sj
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
        quest_id="2026-06-16-bkg111",
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


def test_show_summary_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    summary = ch.quest_dir / "artifacts" / "summary"
    summary.mkdir(parents=True)
    (summary / f"{_ID}.md").write_text("# SUMMARY BODY\n")
    assert "SUMMARY BODY" in _run(monkeypatch, tmp_path, "show", _ID, "summary")


def test_show_default_card_renders_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    summary = ch.quest_dir / "artifacts" / "summary"
    summary.mkdir(parents=True)
    (summary / f"{_ID}.md").write_text(
        "Gives every change a durable intent-level summary.\n")
    out = _run(monkeypatch, tmp_path, "show", _ID)  # default card, no resource
    assert "intent-level summary" in out


def test_show_summary_falls_back_after_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    summary = ch.quest_dir / "artifacts" / "summary"
    summary.mkdir(parents=True)
    (summary / f"{_ID}.md").write_text("# SUMMARY BODY\n")
    files = {
        str(p.relative_to(ch.quest_dir / "artifacts")): p
        for p in sorted((ch.quest_dir / "artifacts").rglob("*"))
        if p.is_file()
    }
    worktree.artifact_commit(tmp_path, _ID, files, message="artifact: summary")
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
    archive.archive_quest(
        tmp_path,
        _ID,
        events_path=ch.events_path,
        quest_json_path=ch.quest_dir / "change.json",
        state_json_path=ch.state_dir / "state.json",
    )
    assert _git_wip_present(tmp_path, _ID)
    worktree.remove_worktree(tmp_path, _ID)
    assert not (tmp_path / ".imp" / "worktrees" / _ID).exists()
    assert "SUMMARY BODY" in _run(monkeypatch, tmp_path, "show", _ID, "summary")


def test_show_default_card_renders_summary_after_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    summary = ch.quest_dir / "artifacts" / "summary"
    summary.mkdir(parents=True)
    (summary / f"{_ID}.md").write_text("# SUMMARY BODY\n")
    files = {
        str(p.relative_to(ch.quest_dir / "artifacts")): p
        for p in sorted((ch.quest_dir / "artifacts").rglob("*"))
        if p.is_file()
    }
    worktree.artifact_commit(tmp_path, _ID, files, message="artifact: summary")
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
    archive.archive_quest(
        tmp_path,
        _ID,
        events_path=ch.events_path,
        quest_json_path=ch.quest_dir / "change.json",
        state_json_path=ch.state_dir / "state.json",
    )
    assert _git_wip_present(tmp_path, _ID)
    worktree.remove_worktree(tmp_path, _ID)
    assert not (tmp_path / ".imp" / "worktrees" / _ID).exists()
    assert "SUMMARY BODY" in _run(monkeypatch, tmp_path, "show", _ID)


def test_filter_events_type_set_selects_named_types() -> None:
    from imp.cli.main import _filter_events

    events = [
        {"type": "user_message", "agent_id": "a"},
        {"type": "tool_call", "agent_id": "b"},
        {"type": "llm_response", "agent_id": "c"},
    ]
    out = _filter_events(events, types="tool_call,llm_response")
    assert [e["type"] for e in out] == ["tool_call", "llm_response"]


def test_filter_events_agent_is_substring_match() -> None:
    from imp.cli.main import _filter_events

    events = [
        {"type": "x", "agent_id": "step:plan/plan-writer"},
        {"type": "y", "agent_id": "sub:abc123"},
        {"type": "z", "agent_id": None},
    ]
    out = _filter_events(events, agent="sub:")
    assert [e["type"] for e in out] == ["y"]


def test_filter_events_limit_keeps_last_n() -> None:
    from imp.cli.main import _filter_events

    events = [{"type": "tick", "agent_id": "a", "seq": i} for i in range(5)]
    out = _filter_events(events, limit=2)
    assert [e["seq"] for e in out] == [3, 4]


def test_filter_events_no_limit_returns_all() -> None:
    from imp.cli.main import _filter_events

    events = [{"type": "tick", "agent_id": "a"} for _ in range(5)]
    assert len(_filter_events(events)) == 5


def test_filter_events_combines_type_agent_then_limit() -> None:
    from imp.cli.main import _filter_events

    events = [
        {"type": "tool_call", "agent_id": "sub:1"},
        {"type": "tool_call", "agent_id": "step:plan"},
        {"type": "tool_call", "agent_id": "sub:2"},
        {"type": "user_message", "agent_id": "sub:3"},
    ]
    # type==tool_call AND agent contains "sub:" -> [sub:1, sub:2]; last 1 -> [sub:2].
    out = _filter_events(events, types="tool_call", agent="sub:", limit=1)
    assert len(out) == 1
    assert out[0]["agent_id"] == "sub:2"


def test_filter_events_non_positive_limit_returns_all() -> None:
    from imp.cli.main import _filter_events

    events = [{"type": "tick", "agent_id": "a"} for _ in range(5)]
    assert len(_filter_events(events, limit=0)) == 5
    assert len(_filter_events(events, limit=-1)) == 5


def _seed_events(ch: Change, events: list[dict]) -> None:
    with ch.events_path.open("a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def test_show_events_digest_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    _seed_events(ch, [
        {"type": "user_message", "agent_id": "step:plan/plan-writer",
         "timestamp": "2026-06-01T00:00:00+00:00",
         "payload": {"role": "user",
                     "content": [{"type": "text", "text": "hello world"}]}},
    ])
    out = _run(monkeypatch, tmp_path, "show", _ID, "events")
    # A digest line: envelope + summarized text, NOT a raw JSONL dump.
    assert "user_message" in out
    assert "hello world" in out
    assert '{"type"' not in out


def test_show_events_full_prints_raw_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    _seed_events(ch, [
        {"type": "user_message", "agent_id": "a",
         "timestamp": "2026-06-01T00:00:00+00:00",
         "payload": {"role": "user",
                     "content": [{"type": "text", "text": "hi"}]}},
    ])
    # --type isolates the seeded event from the change_created event that
    # Change.create writes; --full then renders it as raw JSON via full_line.
    out = _run(monkeypatch, tmp_path, "show", _ID, "events",
               "--type", "user_message", "--full")
    lines = [ln for ln in out.splitlines() if ln]
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["type"] == "user_message"
    assert obj["payload"]["content"][0]["text"] == "hi"


def test_show_events_full_redacts_step_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    _seed_events(ch, [
        {"type": "step_context", "agent_id": "step:plan/plan-writer",
         "timestamp": "2026-06-01T00:00:00+00:00",
         "payload": {"provider": "p", "model": "m",
                     "system_prompt": "TOP SECRET PROMPT",
                     "tools_schema": [{"name": "x"}]}},
    ])
    out = _run(monkeypatch, tmp_path, "show", _ID, "events", "--full")
    assert "TOP SECRET PROMPT" not in out
    assert "[redacted]" in out


def test_show_events_type_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    _seed_events(ch, [
        {"type": "user_message", "agent_id": "a",
         "timestamp": "2026-06-01T00:00:00+00:00",
         "payload": {"role": "user",
                     "content": [{"type": "text", "text": "keep-me"}]}},
        {"type": "tool_call", "agent_id": "b",
         "timestamp": "2026-06-01T00:00:01+00:00",
         "payload": {"id": "t1", "name": "read_file", "input": {}}},
    ])
    out = _run(monkeypatch, tmp_path, "show", _ID, "events",
               "--type", "user_message")
    assert "keep-me" in out
    assert "read_file" not in out


def test_show_events_agent_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    _seed_events(ch, [
        {"type": "tool_call", "agent_id": "step:plan/plan-writer",
         "timestamp": "2026-06-01T00:00:00+00:00",
         "payload": {"id": "t1", "name": "read_file", "input": {}}},
        {"type": "tool_call", "agent_id": "sub:abc123",
         "timestamp": "2026-06-01T00:00:01+00:00",
         "payload": {"id": "t2", "name": "write_file", "input": {}}},
    ])
    out = _run(monkeypatch, tmp_path, "show", _ID, "events", "--agent", "sub:")
    assert "write_file" in out
    assert "read_file" not in out


def test_show_events_limit_keeps_last_n(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    _seed_events(ch, [
        {"type": "tick", "agent_id": "a",
         "timestamp": f"2026-06-01T00:00:0{i}+00:00",
         "payload": {"seq": i}}
        for i in range(5)
    ])
    out = _run(monkeypatch, tmp_path, "show", _ID, "events", "--limit", "2")
    lines = [ln for ln in out.splitlines() if ln]
    assert len(lines) == 2
    assert "seq=3" in out
    assert "seq=4" in out
    assert "seq=0" not in out


def test_show_events_digest_from_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    events = tmp_path / "arch-events.jsonl"
    events.write_text(
        json.dumps({
            "id": "arch01", "type": "user_message",
            "agent_id": "step:plan/plan-writer",
            "timestamp": "2026-06-01T00:00:00+00:00",
            "payload": {"role": "user",
                        "content": [{"type": "text", "text": "archived hello"}]},
        }) + "\n"
    )
    cj = tmp_path / "arch-change.json"
    cj.write_text(json.dumps({
        "id": "arch01", "title": "Arch", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": [],
    }))
    sj = tmp_path / "arch-state.json"
    sj.write_text(json.dumps({"id": "arch01", "status": "completed", "step": None}))
    archive.archive_quest(
        tmp_path, "arch01", events_path=events, quest_json_path=cj,
        state_json_path=sj,
    )
    out = _run(monkeypatch, tmp_path, "show", "arch01", "events")
    assert "user_message" in out
    assert "archived hello" in out
    assert '{"type"' not in out
