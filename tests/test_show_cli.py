"""imp show reads live artifacts, then the archive after merge."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from imp.cli.main import main
from imp.engine import archive, worktree
from imp.engine.quest import Quest
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


def test_show_rejects_review_resource(
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
        quest_json_path=ch.quest_dir / "quest.json",
        state_json_path=ch.state_dir / "state.json",
    )
    # The per-Quest artifact ref is RETAINED at archive (spec AC 3).
    assert _git_wip_present(tmp_path, _ID)
    worktree.remove_worktree(tmp_path, _ID)
    assert not (tmp_path / ".imp" / "worktrees" / _ID).exists()
    # Worktree gone -> spec resolves from the archive.
    assert "SPEC BODY" in _run(monkeypatch, tmp_path, "show", _ID, "spec")


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


def test_show_backlog_spec_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    assert "write_spec" not in out  # never the raw step slug


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
    sj.write_text(json.dumps({"id": "ghost01", "status": "merged", "step": None}))
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
    assert not (tmp_path / ".imp" / "quests" / "2026-06-16-bkg111").exists()
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
    out = _run(monkeypatch, tmp_path, "show", _ID)  # default card, no resource
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
    assert not (tmp_path / ".imp" / "worktrees" / _ID).exists()
    assert "SUMMARY BODY" in _run(monkeypatch, tmp_path, "show", _ID)


def _seed_events(ch: Quest, events: list[dict]) -> None:
    with ch.events_path.open("a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def test_show_rejects_events_resource(
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
        "id": "depa01", "status": "merged", "step": None,
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
    monkeypatch.setattr(m, "_iter_quest_ids", lambda control: ["idea01"])
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
    monkeypatch.setattr(m, "_iter_quest_ids", lambda control: ["idea02"])
    out = _run(monkeypatch, tmp_path, "show", "idea02", "--json")
    data = json.loads(out)
    assert data["body"] == "wire up OAuth login"


def test_show_json_none_quest_prints_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "_iter_quest_ids", lambda control: ["lostid"])
    monkeypatch.setattr(m, "_resolve_id", lambda control, token: "lostid")
    monkeypatch.setattr(m, "_unstarted_backlog_meta", lambda control, qid: None)
    monkeypatch.setattr(m, "_row_for_id", lambda control, qid: None)
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
    phome = tmp_path / ".imp" / "quests" / "prnt10"
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
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Gate", quest_id="mbk01", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "merge_blocked"
    st.save(ch.state_dir)
    card = "\n".join(_quest_card(tmp_path, "mbk01", now=datetime.now(UTC), color=False))
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
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Broke", quest_id="err02", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "error"
    st.step = "task-lead"
    st.save(ch.state_dir)
    card = "\n".join(_quest_card(tmp_path, "err02", now=datetime.now(UTC), color=False))
    status_line = next(ln for ln in card.splitlines() if ln.startswith("status:"))
    assert "error" in status_line
    assert any(ln.startswith("step:") for ln in card.splitlines())   # error is in-flight
