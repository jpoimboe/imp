"""imp show reads live artifacts, then the archive after merge."""

from __future__ import annotations

import io
import json
import os
import shutil
import time
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from imp.cli.main import main
from imp.engine import archive, worktree
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.paths import project_state_dir
from tests.conftest import _seed_control, _seed_control_with_vars

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


def _seed_with_artifacts(control: Path, quest_id: str, title: str) -> Quest:
    ch = Quest.create(control, title=title, quest_id=quest_id, base_branch="main")
    spec = ch.quest_dir / "artifacts" / "spec"
    spec.mkdir(parents=True)
    (spec / f"{quest_id}.md").write_text("# SPEC BODY\n")
    plans = ch.quest_dir / "artifacts" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{quest_id}-plan-1.md").write_text("# PLAN ONE\n")
    (plans / f"{quest_id}-plan-2.md").write_text("# PLAN TWO\n")
    return ch


_FAILURE_LINE = "E   assert probe_advertised_tools() == {}"
_VERIFY_LOG = (
    "# Gate failure — fast\n\n"
    "- command: make check\n"
    "- exit: 2\n"
    "- when: 2026-09-04T18:22:31Z\n\n"
    f"{_FAILURE_LINE}\n"
)


def _seed_verify_log(ch: Quest, quest_id: str) -> None:
    verify = ch.quest_dir / "artifacts" / "verify"
    verify.mkdir(parents=True)
    (verify / f"{quest_id}.md").write_text(_VERIFY_LOG)


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


def test_show_rejects_review_artifact(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["show", _ID, "review"])
    assert "invalid choice: 'review'" in capsys.readouterr().err


@pytest.mark.parametrize("facet", ["plan", "status", "summary"])
def test_show_rejects_trimmed_facets(
    facet: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["show", _ID, facet])
    assert f"invalid choice: '{facet}'" in capsys.readouterr().err


def test_show_invalid_artifact_error_names_artifact(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["show", _ID, "status"])
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "argument artifact: invalid choice: 'status'" in err
    assert "resource" not in err


def _archive_and_prune(control: Path, ch: Quest, quest_id: str) -> None:
    """Prunes the home's artifacts too: `_show_artifact` prefers the live file, so
    leaving it would resolve without ever reading the archive."""
    files = {
        str(p.relative_to(ch.quest_dir / "artifacts")): p
        for p in sorted((ch.quest_dir / "artifacts").rglob("*"))
        if p.is_file()
    }
    worktree.artifact_commit(control, quest_id, files, message="artifact: seed")
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
        control,
        quest_id,
        events_path=ch.events_path,
        quest_json_path=ch.quest_dir / "quest.json",
        state_json_path=ch.state_dir / "state.json",
    )
    # The per-Quest artifact ref is RETAINED at archive (spec AC 3).
    assert _git_wip_present(control, quest_id)
    worktree.remove_worktree(control, quest_id)
    assert not (project_state_dir(control) / "worktrees" / quest_id).exists()
    shutil.rmtree(ch.quest_dir / "artifacts")


def test_show_spec_falls_back_after_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    _archive_and_prune(tmp_path, ch, _ID)
    # Live files gone -> spec resolves from refs/imp/artifacts/<id>.
    assert "SPEC BODY" in _run(monkeypatch, tmp_path, "show", _ID, "spec")


def test_show_verify_falls_back_after_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    _seed_verify_log(ch, _ID)
    _archive_and_prune(tmp_path, ch, _ID)
    assert _FAILURE_LINE in _run(monkeypatch, tmp_path, "show", _ID, "verify")


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
    Quest.create_draft(
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


def test_show_backlog_artifact_message_wording(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    Quest.create_draft(
        tmp_path,
        title="Add OAuth",
        quest_id="2026-06-16-bkg111",
        base_branch="main",
        origin="idea",
        seed_opening="Add OAuth",
    )
    rc_spec = main(["show", "2026-06-16-bkg111", "spec"])
    err_spec = capsys.readouterr().err
    assert rc_spec == 2
    assert err_spec.rstrip("\n") == (
        "imp: 2026-06-16-bkg111 is in the backlog (not started); no spec yet"
    )

    rc_verify = main(["show", "2026-06-16-bkg111", "verify"])
    err_verify = capsys.readouterr().err
    assert rc_verify == 2
    assert err_verify.rstrip("\n") == (
        "imp: 2026-06-16-bkg111 is in the backlog (not started); no failed test run yet"
    )


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
    joined = "\n".join(_backlog_card(meta, tmp_path, now=datetime.now(UTC), color=False))
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
    lines = _backlog_card(meta, tmp_path, now=datetime.now(UTC), color=False)
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
    lines = _backlog_card(meta, tmp_path, now=datetime.now(UTC), color=False)
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
    ch = Quest.create(tmp_path, title="Feat", quest_id="shwstp", base_branch="main")
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
    step_line = next(ln for ln in out.splitlines() if ln.startswith("step:"))
    assert "write_spec" not in step_line  # never the raw step slug on the step: line


def test_show_step_line_omits_phase_when_no_phase_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No step_started event carries a phase_label -> show the step label alone,
    # with no raw phase slug and no leading separator.
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Feat", quest_id="shwnop", base_branch="main")
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
    ch = Quest.create(tmp_path, title="Feat", quest_id="shwstl", base_branch="main")
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
    ch = Quest.create(tmp_path, title="Feat", quest_id="shwtrn", base_branch="main")
    st = State.load(ch.state_dir)
    st.step = "write_spec"
    st.step_label = "writing spec " + "x" * 200  # far wider than the 80-col test terminal
    st.save(ch.state_dir)
    out = _run(monkeypatch, tmp_path, "show", "shwtrn")
    step_line = next(ln for ln in out.splitlines() if ln.startswith("step:"))
    assert len(step_line) <= 80  # clipped to the (test) 80-col terminal width
    assert step_line.endswith("…")


def test_show_spec_from_wip_ref_when_home_absent(
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
        '"type":"quest_created","payload":{}}\n'
    )
    cj = tmp_path / "ghost-quest.json"
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


def test_show_abandoned_backlog_is_not_a_live_brief(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as m
    from imp.engine.abandon import tear_down_quest

    _seed_control(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    quest = Quest.create_draft(
        tmp_path,
        title="Add OAuth",
        quest_id="2026-06-16-bkg111",
        base_branch="main",
        origin="idea",
        seed_opening="Add OAuth",
    )
    tear_down_quest(quest)
    assert not (project_state_dir(tmp_path) / "quests" / "2026-06-16-bkg111").exists()
    out = _run(monkeypatch, tmp_path, "show", "2026-06-16-bkg111")
    assert "id:" in out
    assert "readiness:" not in out  # detail card, not a queued Backlog brief
    # log renders the archived conversation, not a brief
    assert main(["log", "2026-06-16-bkg111"]) == 0


def test_show_default_card_renders_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    summary = ch.quest_dir / "artifacts" / "summary"
    summary.mkdir(parents=True)
    (summary / f"{_ID}.md").write_text(
        "Gives every change a durable intent-level summary.\n")
    out = _run(monkeypatch, tmp_path, "show", _ID)  # default card, no artifact
    assert "intent-level summary" in out


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
        quest_json_path=ch.quest_dir / "quest.json",
        state_json_path=ch.state_dir / "state.json",
    )
    assert _git_wip_present(tmp_path, _ID)
    worktree.remove_worktree(tmp_path, _ID)
    assert not (project_state_dir(tmp_path) / "worktrees" / _ID).exists()
    assert "SUMMARY BODY" in _run(monkeypatch, tmp_path, "show", _ID)


def _seed_events(ch: Quest, events: list[dict]) -> None:
    with ch.events_path.open("a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def test_show_rejects_events_artifact(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["show", _ID, "events"])
    assert "invalid choice: 'events'" in capsys.readouterr().err


def test_show_json_emits_structured_overview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    # Archive a dependency so its title resolves and merged=True.
    dep_events = tmp_path / "dep.jsonl"
    dep_events.write_text("")
    dep_qj = tmp_path / "dep.quest.json"
    dep_qj.write_text(json.dumps({
        "id": "depa01", "title": "Dep A", "base_branch": "main",
        "created_at": "2026-06-20T11:00:00+00:00", "depends_on": []}))
    dep_sj = tmp_path / "dep.state.json"
    dep_sj.write_text(json.dumps({
        "id": "depa01", "status": "completed", "step": None,
        "merge_commit": "abc123"}))
    archive.archive_quest(tmp_path, "depa01", events_path=dep_events,
                          quest_json_path=dep_qj, state_json_path=dep_sj)
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["depends_on"] = ["depa01"]
    meta_path.write_text(json.dumps(meta))
    with ch.events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "user_message", "agent_id": None,
            "payload": {"role": "user", "subtype": "intent",
                        "content": [{"type": "text", "text": "wire up the widget"}]}}) + "\n")
    out = _run(monkeypatch, tmp_path, "show", _ID, "--json")
    data = json.loads(out)
    assert data["id"] == _ID
    assert data["title"] == "Feat"
    assert data["status"] == "new"
    assert data["intent"] == "wire up the widget"
    assert data["depends_on"] == [
        {"id": "depa01", "title": "Dep A", "merged": True}]
    assert data["created"] is not None


def test_show_json_backlog_quest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    meta = {
        "id": "idea01", "title": "Add OAuth", "origin": "idea",
        "materialized": False, "depends_on": [],
        "created_at": "2026-06-20T11:00:00+00:00",
        "seed_opening": "wire up OAuth login",
    }
    monkeypatch.setattr(m, "_unstarted_backlog_meta", lambda control, qid: meta)
    monkeypatch.setattr(m, "_resolve_id", lambda control, token: "idea01")
    out = _run(monkeypatch, tmp_path, "show", "idea01", "--json")
    data = json.loads(out)
    assert data["id"] == "idea01"
    assert data["status"] == "backlog"
    assert data["provenance"] == "idea"
    assert data["readiness"] == "ready"
    assert data["body"] == "wire up OAuth login"


def test_show_json_backlog_body_strips_title_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    meta = {
        "id": "idea02", "title": "Add OAuth", "origin": "idea",
        "materialized": False, "depends_on": [],
        "created_at": "2026-06-20T11:00:00+00:00",
        "seed_opening": "Add OAuth\nwire up OAuth login",
    }
    monkeypatch.setattr(m, "_unstarted_backlog_meta", lambda control, qid: meta)
    monkeypatch.setattr(m, "_resolve_id", lambda control, token: "idea02")
    out = _run(monkeypatch, tmp_path, "show", "idea02", "--json")
    data = json.loads(out)
    assert data["body"] == "wire up OAuth login"


def test_show_json_none_quest_prints_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import imp.cli.main as m
    import imp.cli.store as s

    _seed_control(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_resolve_id", lambda control, token: "lostid")
    monkeypatch.setattr(m, "_unstarted_backlog_meta", lambda control, qid: None)
    monkeypatch.setattr(s, "_row_for_id", lambda control, qid: None)
    rc = main(["show", "lostid", "--json"])
    assert rc == 2
    assert "no such quest" in capsys.readouterr().err


def test_card_field_dims_label_and_aligns_value() -> None:
    from imp.cli import main

    field = main._card_field("id:", "abc123", 15, color=True)
    assert field == "\x1b[2mid:\x1b[0m" + " " * 12 + "abc123"
    assert "\x1b[2m" in field                 # dim SGR wraps the label
    assert field.endswith("abc123")           # value stays plain, no trailing SGR


def test_card_field_plain_when_color_disabled() -> None:
    from imp.cli import main

    field = main._card_field("provenance:", "idea", 12, color=False)
    assert field == "provenance: idea"        # exactly one pad space, no escapes
    assert "\x1b[" not in field


def test_backlog_card_dims_labels_and_intent_header(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from imp.cli import palette as pal
    from imp.cli.main import _backlog_card

    _seed_control(tmp_path)
    meta = {
        "id": "bk009", "title": "Do X", "origin": "idea",
        "depends_on": [], "created_at": None,
        "seed_opening": "Do X\n\nbody prose",
    }
    lines = _backlog_card(meta, tmp_path, now=datetime.now(UTC), color=True)
    joined = "\n".join(lines)
    assert pal.ansi("id:", "dim") in joined            # metadata label dimmed
    assert pal.ansi("provenance:", "dim") in joined
    assert pal.ansi("intent:", "dim") in lines          # dim intent header line
    idx = lines.index(pal.ansi("intent:", "dim"))
    assert lines[idx - 1] == ""                          # one blank line precedes it
    assert lines[idx + 1] == "body prose"                # body prose follows header


def test_backlog_card_plain_without_color_keeps_header(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from imp.cli.main import _backlog_card

    _seed_control(tmp_path)
    meta = {
        "id": "bk010", "title": "Do X", "origin": "idea",
        "depends_on": [], "created_at": None,
        "seed_opening": "Do X\n\nbody prose",
    }
    lines = _backlog_card(meta, tmp_path, now=datetime.now(UTC), color=False)
    joined = "\n".join(lines)
    assert "\x1b[" not in joined                          # no SGR escapes anywhere
    assert "intent:" in lines                             # header text still present
    assert "provenance: idea" in joined                  # label plain, value aligned


def test_show_backlog_card_threads_color(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from imp.cli import palette as pal
    from imp.engine.quest import Quest

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    _seed_control(tmp_path)
    Quest.create_draft(
        tmp_path, title="Add OAuth", quest_id="2026-06-16-bkg333",
        base_branch="main", origin="followup",
        seed_opening="Add OAuth\n\nwire up OAuth")
    out = _run(monkeypatch, tmp_path, "show", "2026-06-16-bkg333")
    assert pal.ansi("id:", "dim") in out                 # label dimmed via threaded color
    assert pal.ansi("intent:", "dim") in out             # dim intent header via threaded color


def test_show_json_includes_parent_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_control(tmp_path)
    phome = project_state_dir(tmp_path) / "quests" / "prnt10"
    phome.mkdir(parents=True)
    (phome / "quest.json").write_text(json.dumps(
        {"id": "prnt10", "title": "Parent Q", "base_branch": "main"}))
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    meta_path = ch.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["parent"] = "prnt10"
    meta_path.write_text(json.dumps(meta))
    out = _run(monkeypatch, tmp_path, "show", _ID, "--json")
    data = json.loads(out)
    assert data["parent"] == {"id": "prnt10", "title": "Parent Q"}


def test_log_backlog_card_threads_color(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from imp.cli import palette as pal
    from imp.engine.quest import Quest

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    _seed_control(tmp_path)
    Quest.create_draft(
        tmp_path, title="Add OAuth", quest_id="2026-06-16-bkg444",
        base_branch="main", origin="followup",
        seed_opening="Add OAuth\n\nwire up OAuth")
    out = _run(monkeypatch, tmp_path, "log", "2026-06-16-bkg444")
    assert pal.ansi("id:", "dim") in out                 # label dimmed via _cmd_log call site
    assert pal.ansi("intent:", "dim") in out             # dim intent header via _cmd_log call site


def test_show_card_renders_merge_blocked_two_words(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from imp.cli.main import _quest_card
    from imp.cli.store import make_store
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Gate", quest_id="mbk01", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "merge_blocked"
    st.save(ch.state_dir)
    qc = make_store(tmp_path).quest_card("mbk01")
    assert qc is not None
    card = "\n".join(_quest_card(qc, now=datetime.now(UTC), color=False, ledger=None))
    status_line = next(ln for ln in card.splitlines() if ln.startswith("status:"))
    assert "merge blocked" in status_line
    assert "merge_blocked" not in status_line


def test_show_json_keeps_merge_blocked_underscore(tmp_path: Path) -> None:
    from imp.cli.main import _quest_json
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Gate", quest_id="mbk02", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "merge_blocked"
    st.save(ch.state_dir)
    data = _quest_json(tmp_path, "mbk02")
    assert data is not None
    assert data["status"] == "merge_blocked"   # byte-clean machine value


def test_show_card_step_line_shows_for_error(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from imp.cli.main import _quest_card
    from imp.cli.store import make_store
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Broke", quest_id="err02", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "error"
    st.step = "task-lead"
    st.save(ch.state_dir)
    qc = make_store(tmp_path).quest_card("err02")
    assert qc is not None
    card = "\n".join(_quest_card(qc, now=datetime.now(UTC), color=False, ledger=None))
    status_line = next(ln for ln in card.splitlines() if ln.startswith("status:"))
    assert "error" in status_line
    assert any(ln.startswith("step:") for ln in card.splitlines())   # error is in-flight


def _quest_with_vars(control: Path) -> str:
    _seed_control_with_vars(control)
    Quest.create_draft(
        control, title="Varred", quest_id="varshow", base_branch="main",
        vars={"jurisdiction": "eu", "retries": 5, "strict": True})
    return "varshow"


def _quest_with_engine_vars(control: Path) -> str:
    """Seeded with one --var; state.vars also holds workflow defaults and engine vars."""
    _seed_control_with_vars(control)
    quest = Quest.create_draft(
        control, title="Engine", quest_id="engvars", base_branch="main",
        vars={"strict": False})
    st = State.load(quest.state_dir)
    st.vars.update({"plan_index": 1, "integration_fork": "abc",
                    "plan_review_notes": "line one\nline two"})
    st.save(quest.state_dir)
    return "engvars"


def test_quest_card_carries_seeded_vars_beside_the_effective_set(
    tmp_path: Path,
) -> None:
    from imp.cli.store import make_store
    quest_id = _quest_with_engine_vars(tmp_path)

    qc = make_store(tmp_path).quest_card(quest_id)

    assert qc is not None
    assert qc.seeded_vars == {"strict": False}
    assert qc.vars == {
        "jurisdiction": "eu", "retries": 3, "strict": False, "plan_index": 1,
        "integration_fork": "abc", "plan_review_notes": "line one\nline two"}


def test_show_card_lists_only_the_vars_seeded_with_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest_id = _quest_with_engine_vars(tmp_path)

    out = _run(monkeypatch, tmp_path, "show", quest_id)

    assert _vars_rows(out) == ["vars:          strict=false"]
    for hidden in ("plan_index", "integration_fork", "plan_review_notes",
                   "jurisdiction", "retries"):
        assert hidden not in out


def test_show_json_keeps_every_effective_var_the_card_hides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest_id = _quest_with_engine_vars(tmp_path)

    data = json.loads(_run(monkeypatch, tmp_path, "show", quest_id, "--json"))

    assert data["vars"] == {
        "jurisdiction": "eu", "retries": 3, "strict": False, "plan_index": 1,
        "integration_fork": "abc", "plan_review_notes": "line one\nline two"}


def test_show_card_omits_vars_when_none_were_seeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The workflow defaults and an engine-set multi-line var are effective, not seeded.
    _seed_control_with_vars(tmp_path)
    quest = Quest.create_draft(
        tmp_path, title="Bare", quest_id="bare01", base_branch="main")
    st = State.load(quest.state_dir)
    st.vars["plan_review_notes"] = "line one\nline two"
    st.save(quest.state_dir)

    out = _run(monkeypatch, tmp_path, "show", "bare01")

    assert not any(ln.startswith("vars") for ln in out.splitlines())


def test_show_card_renders_seeded_vars_sorted_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bools render the way --var accepts them.
    quest_id = _quest_with_vars(tmp_path)

    out = _run(monkeypatch, tmp_path, "show", quest_id)

    line = next(ln for ln in out.splitlines() if ln.startswith("vars:"))
    assert line.split(maxsplit=1)[1] == "jurisdiction=eu, retries=5, strict=true"


def _vars_rows(out: str) -> list[str]:
    """The `vars:` line plus its indented continuation lines."""
    lines = out.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("vars:"))
    end = start + 1
    while end < len(lines) and lines[end].startswith(" "):
        end += 1
    return lines[start:end]


def test_show_card_wraps_the_vars_row_at_entry_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 40 columns leaves 25 for values: entries fill greedily, a line that
    # continues ends in a comma, and nothing is clipped.
    quest_id = _quest_with_vars(tmp_path)
    monkeypatch.setattr("imp.cli.main._term_width", lambda: 40)

    out = _run(monkeypatch, tmp_path, "show", quest_id)

    assert _vars_rows(out) == [
        "vars:          jurisdiction=eu,",
        "               retries=5, strict=true",
    ]
    assert "…" not in out


def test_show_backlog_card_wraps_the_vars_row_under_its_value_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control_with_vars(tmp_path)
    Quest.create_draft(
        tmp_path, title="Queued", quest_id="varbkw", base_branch="main",
        origin="backlog", seed_opening="Queued", defer_seed=True,
        vars={"retries": 5, "strict": True})
    monkeypatch.setattr("imp.cli.main._term_width", lambda: 30)

    out = _run(monkeypatch, tmp_path, "show", "varbkw")

    assert _vars_rows(out) == [
        "vars:       retries=5,",
        "            strict=true",
    ]


def test_show_card_keeps_an_entry_wider_than_the_row_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control_with_vars(tmp_path)
    Quest.create_draft(
        tmp_path, title="Wide", quest_id="varwide", base_branch="main",
        vars={"jurisdiction": "a" * 20, "retries": 5, "strict": True})
    monkeypatch.setattr("imp.cli.main._term_width", lambda: 30)

    out = _run(monkeypatch, tmp_path, "show", "varwide")

    assert _vars_rows(out) == [
        "vars:          jurisdiction=" + "a" * 20 + ",",
        "               retries=5,",
        "               strict=true",
    ]


@pytest.mark.parametrize(("cols", "expected"), [
    (40, ["vars:          jurisdiction=e,", "               retries=5, strict=false"]),
    (41, ["vars:          jurisdiction=e, retries=5,", "               strict=false"]),
])
def test_show_card_counts_the_trailing_comma_when_wrapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    cols: int, expected: list[str],
) -> None:
    # `jurisdiction=e, retries=5` is 25 columns; a non-last entry also needs its comma.
    _seed_control_with_vars(tmp_path)
    Quest.create_draft(
        tmp_path, title="Edge", quest_id="varedge", base_branch="main",
        vars={"jurisdiction": "e", "retries": 5, "strict": False})
    monkeypatch.setattr("imp.cli.main._term_width", lambda: cols)

    out = _run(monkeypatch, tmp_path, "show", "varedge")

    assert _vars_rows(out) == expected


def test_show_card_indents_vars_continuations_without_color_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.cli import palette as pal
    from imp.cli.main import _card_field

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    quest_id = _quest_with_vars(tmp_path)
    monkeypatch.setattr("imp.cli.main._term_width", lambda: 40)

    out = _run(monkeypatch, tmp_path, "show", quest_id)

    lines = out.splitlines()
    first = next(i for i, ln in enumerate(lines) if ln.startswith(pal.ansi("vars:", "dim")))
    assert lines[first] == _card_field("vars:", "jurisdiction=eu,", 15, color=True)
    assert lines[first + 1] == "               retries=5, strict=true"


def test_show_card_prints_the_vars_row_on_one_line_when_piped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Captured stdout is not a tty and $COLUMNS is unset: `imp show x | grep vars:`
    # must return every short var, even past 80 columns.
    monkeypatch.delenv("COLUMNS", raising=False)
    _seed_control_with_vars(tmp_path)
    Quest.create_draft(
        tmp_path, title="Piped", quest_id="varpipe", base_branch="main",
        vars={"jurisdiction": "x" * 70, "retries": 5, "strict": True})

    out = _run(monkeypatch, tmp_path, "show", "varpipe")

    assert _vars_rows(out) == [
        "vars:          jurisdiction=" + "x" * 70 + ", retries=5, strict=true",
    ]


def _card_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_ledger: bool = False,
    **changes: Any,
) -> list[str]:
    """A rendered quest card with `changes` applied to its QuestCard."""
    from dataclasses import replace
    from datetime import UTC, datetime

    from imp.cli.main import _CardLedger, _quest_card
    from imp.cli.store import make_store
    monkeypatch.setattr("imp.cli.main._term_width", lambda: 500)
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Long", quest_id="lng01", base_branch="main")
    qc = make_store(tmp_path).quest_card("lng01")
    assert qc is not None
    ledger = _CardLedger(run="1m", body=["LEDGER ROW"]) if with_ledger else None
    return _quest_card(replace(qc, **changes), now=datetime.now(UTC), color=False,
                       ledger=ledger)


def test_show_card_moves_a_multiline_var_into_its_own_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control_with_vars(tmp_path)
    Quest.create_draft(
        tmp_path, title="Notes", quest_id="varml", base_branch="main",
        vars={"jurisdiction": "para one\n\npara two", "retries": 5})

    out = _run(monkeypatch, tmp_path, "show", "varml")

    lines = out.splitlines()
    assert _vars_rows(out) == ["vars:          retries=5"]
    i = lines.index("vars.jurisdiction:")
    assert lines[i - 1:i + 4] == ["", "vars.jurisdiction:", "para one", "", "para two"]
    assert json.loads(_run(monkeypatch, tmp_path, "show", "varml", "--json"))[
        "vars"]["jurisdiction"] == "para one\n\npara two"


def test_show_card_places_var_blocks_between_summary_and_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Multi-line and >80-char values both leave the row; blocks sort by name.
    lines = _card_lines(
        tmp_path, monkeypatch, with_ledger=True, summary=b"Shipped.\n",
        seeded_vars={"notes": "para one\n\npara two\n", "retries": 5, "zeta": "y" * 81})

    assert next(ln for ln in lines if ln.startswith("vars:")) == "vars:          retries=5"
    assert lines[lines.index("summary:"):] == [
        "summary:", "Shipped.",
        "", "vars.notes:", "para one", "", "para two",
        "", "vars.zeta:", "y" * 81,
        "", "ledger:", "LEDGER ROW",
    ]


def test_show_card_keeps_short_and_empty_values_on_the_vars_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exactly 80 characters is still short; an empty value stays inline.
    lines = _card_lines(
        tmp_path, monkeypatch,
        seeded_vars={"edge": "e" * 80, "plan_review_notes": "", "retries": 5})

    assert next(ln for ln in lines if ln.startswith("vars:")) == (
        "vars:          edge=" + "e" * 80 + ", plan_review_notes=, retries=5")
    assert not any(ln.startswith("vars.") for ln in lines)


def test_show_card_omits_the_vars_row_when_every_var_is_long(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = _card_lines(tmp_path, monkeypatch, seeded_vars={"notes": "a\nb"})

    assert not any(ln.startswith("vars:") for ln in lines)
    i = lines.index("vars.notes:")
    assert lines[i - 1:i + 3] == ["", "vars.notes:", "a", "b"]


def test_show_backlog_card_puts_var_blocks_after_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control_with_vars(tmp_path)
    Quest.create_draft(
        tmp_path, title="Queued", quest_id="varbkl", base_branch="main",
        origin="backlog", seed_opening="Queued\n\nDo the thing.", defer_seed=True,
        vars={"jurisdiction": "para one\n\npara two", "retries": 5})

    out = _run(monkeypatch, tmp_path, "show", "varbkl")

    lines = out.splitlines()
    assert _vars_rows(out) == ["vars:       retries=5"]
    i = lines.index("vars.jurisdiction:")
    assert lines.index("intent:") < i
    assert lines[i - 1:i + 4] == ["", "vars.jurisdiction:", "para one", "", "para two"]


def test_show_backlog_card_renders_the_supplied_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Filed with --var and never run: the overrides live on quest.json, and the
    # backlog card is where the user looks for them.
    _seed_control_with_vars(tmp_path)
    Quest.create_draft(
        tmp_path, title="Queued", quest_id="varbk", base_branch="main",
        origin="backlog", seed_opening="Queued", defer_seed=True,
        vars={"retries": 5, "strict": True})

    out = _run(monkeypatch, tmp_path, "show", "varbk")

    line = next(ln for ln in out.splitlines() if ln.startswith("vars:"))
    assert line.split(maxsplit=1)[1] == "retries=5, strict=true"
    assert json.loads(
        _run(monkeypatch, tmp_path, "show", "varbk", "--json"))["vars"] == {
            "retries": 5, "strict": True}


def test_show_backlog_card_omits_the_vars_line_when_none_were_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control_with_vars(tmp_path)
    Quest.create_draft(
        tmp_path, title="Queued", quest_id="plainbk", base_branch="main",
        origin="backlog", seed_opening="Queued", defer_seed=True)

    out = _run(monkeypatch, tmp_path, "show", "plainbk")

    assert "vars:" not in out
    assert json.loads(
        _run(monkeypatch, tmp_path, "show", "plainbk", "--json"))["vars"] == {}


def test_show_json_includes_the_effective_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    quest_id = _quest_with_vars(tmp_path)

    data = json.loads(_run(monkeypatch, tmp_path, "show", quest_id, "--json"))

    assert data["vars"] == {"jurisdiction": "eu", "retries": 5, "strict": True}


def test_show_omits_the_vars_line_when_the_workflow_declares_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Plain", quest_id="novars", base_branch="main")

    out = _run(monkeypatch, tmp_path, "show", "novars")

    assert "vars:" not in out
    assert json.loads(
        _run(monkeypatch, tmp_path, "show", "novars", "--json"))["vars"] == {}


def test_show_verify_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the artifact is the assertion text the feedback excerpt
    drops, so the read has to surface the log body, not the spec."""
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    _seed_verify_log(ch, _ID)
    out = _run(monkeypatch, tmp_path, "show", _ID, "verify")
    assert _FAILURE_LINE in out
    assert "# Gate failure — fast" in out
    assert "SPEC BODY" not in out


def test_show_verify_absent_reports_no_gate_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    _seed_with_artifacts(tmp_path, _ID, "Feat")  # spec present, no gate has failed
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    rc = main(["show", _ID, "verify"])
    err = capsys.readouterr().err
    assert rc == 2, err
    assert err.rstrip("\n") == f"imp: no failed test run recorded for '{_ID}'"


def test_show_absent_spec_message_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Feat", quest_id=_ID, base_branch="main")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    rc = main(["show", _ID, "spec"])
    err = capsys.readouterr().err
    assert rc == 2, err
    assert err.rstrip("\n") == f"imp: no spec for '{_ID}'"


@pytest.fixture
def chicago_tz():
    prev = os.environ.get("TZ")
    os.environ["TZ"] = "America/Chicago"   # September → CDT (UTC-5)
    time.tzset()
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev
        time.tzset()


_LOG_WITH_INNER_WHEN = (
    "# Gate failure — fast\n\n"
    "- command: make check\n"
    "- exit: 2\n"
    "- when: 2026-09-04T18:22:31Z\n\n"
    f"{_FAILURE_LINE}\n"
    "- when: 2026-01-01T00:00:00Z\n"
)


def test_show_verify_renders_header_when_in_local_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chicago_tz: None,
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    verify = ch.quest_dir / "artifacts" / "verify"
    verify.mkdir(parents=True)
    (verify / f"{_ID}.md").write_text(_LOG_WITH_INNER_WHEN)

    out = _run(monkeypatch, tmp_path, "show", _ID, "verify")

    assert out.splitlines() == [
        "# Gate failure — fast",
        "",
        "- command: make check",
        "- exit: 2",
        "- when: 2026-09-04 13:22:31",
        "",
        _FAILURE_LINE,
        "- when: 2026-01-01T00:00:00Z",   # gate output is never rewritten
    ]


def test_show_spec_does_not_rewrite_timestamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chicago_tz: None,
) -> None:
    _seed_control(tmp_path)
    ch = _seed_with_artifacts(tmp_path, _ID, "Feat")
    (ch.quest_dir / "artifacts" / "spec" / f"{_ID}.md").write_text(_LOG_WITH_INNER_WHEN)

    out = _run(monkeypatch, tmp_path, "show", _ID, "spec")

    assert out == _LOG_WITH_INNER_WHEN
