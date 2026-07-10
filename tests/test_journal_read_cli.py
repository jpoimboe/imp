"""imp log / imp list / imp show against an ARCHIVED (merged,
worktree-removed) Change.

Seeds the per-Change archive directly (the sole read source for completed
Changes), then drives the CLI. No live providers needed.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from imp.cli.main import main
from imp.engine import archive, worktree
from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _seed_control

# A phase that renders through the narrative — the proof the archive was read.
# (change_created emits no line now that the slug/title banner is gone, so we
# need a real rendered node to assert on.)
_MARKER_PHASE = "summary"


def _seed_marker_phase(control: Path, slug: str) -> None:
    """Append a step_started event so the change has a node that renders through
    the narrative. Reuses the change's own first-event timestamp so it never
    perturbs last-activity sorting. Call BEFORE _complete_and_archive."""
    ch = Change.load(control, slug)
    with ch.events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "id": "marker", "agent_id": None,
            "timestamp": "2026-05-27T10:00:01+00:00",
            "type": "step_started",
            "payload": {"step": "s", "kind": "agent", "phase": _MARKER_PHASE,
                        "model": "m", "provider": "anthropic"},
        }) + "\n")


def _complete_and_archive(control: Path, slug: str, status: str = "completed") -> None:
    """Flip the Change to <status>, archive its runtime snapshot, then remove the
    worktree — the exact post-merge state imp log/status/show read from."""
    ch = Change.load(control, slug)
    st = State.load(ch.state_dir)
    st.status = status
    st.save(ch.state_dir)
    archive.archive_quest(
        control, slug,
        events_path=ch.events_path,
        quest_json_path=ch.quest_dir / "quest.json",
        state_json_path=ch.state_dir / "state.json")
    worktree.remove_worktree(control, slug)


def test_log_reads_from_archive_after_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Done Thing", quest_id="done-thing", base_branch="main")
    _seed_marker_phase(tmp_path, "done-thing")
    _complete_and_archive(tmp_path, "done-thing")
    # Worktree is gone: Change.load now raises; log must use the archive.
    assert not (tmp_path / ".imp" / "worktrees" / "done-thing").exists()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["log", "done-thing"])
    assert rc == 0
    assert _MARKER_PHASE in buf.getvalue()


def test_log_unknown_slug_returns_rc_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    rc = main(["log", "ghost"])
    assert rc == 2
    assert "ghost" in capsys.readouterr().err


def test_log_no_slug_resolves_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Only One", quest_id="only-one", base_branch="main")
    _seed_marker_phase(tmp_path, "only-one")
    _complete_and_archive(tmp_path, "only-one")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["log"])  # no slug, no in-flight worktrees
    assert rc == 0
    assert _MARKER_PHASE in buf.getvalue()


def test_status_shows_completed_section_from_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    # One in-flight, one merged-and-archived.
    Change.create(tmp_path, title="Live One", quest_id="live-one", base_branch="main")
    Change.create(tmp_path, title="Gone One", quest_id="gone-one", base_branch="main")
    _complete_and_archive(tmp_path, "gone-one")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    rc = m._cmd_status(type("NS", (), {"all": True})())
    assert rc == 0
    out = capsys.readouterr().out
    # in-flight slug appears under in-flight; merged slug under completed.
    assert "live-one" in out
    assert "gone-one" in out
    # The merged Change shows its title + merged disposition, not blank columns.
    assert "Gone One" in out
    assert "merged" in out


def test_status_inflight_takes_precedence_over_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If a slug is BOTH a live worktree and archived (crash between archive and
    worktree removal), the completed section must not double-list it."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Both", quest_id="both", base_branch="main")
    # Archive it but DON'T remove the worktree (simulates the crash window).
    archive.archive_quest(
        tmp_path, "both",
        events_path=ch.events_path,
        quest_json_path=ch.quest_dir / "quest.json",
        state_json_path=ch.state_dir / "state.json")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    completed = m._completed_rows(tmp_path)
    assert [r[0] for r in completed] == []  # in-flight worktree wins
    rc = m._cmd_status(type("NS", (), {"all": True})())
    assert rc == 0
    out = capsys.readouterr().out
    # The change appears only once (in-flight section wins; no duplicate in past).
    data_rows = [ln for ln in out.splitlines() if "both" in ln and "HANDLE" not in ln]
    assert len(data_rows) == 1


def test_sort_rows_newest_first() -> None:
    import imp.cli.main as m
    # (id, status, step, last_iso, title, created_at)
    rows = [
        ("old", "completed", "-", "2024-01-01T00:00:00+00:00", "Old", "2024-01-01T00:00:00+00:00"),
        ("new", "completed", "-", "2026-01-01T00:00:00+00:00", "New", "2026-01-01T00:00:00+00:00"),
        ("mid", "completed", "-", "2025-01-01T00:00:00+00:00", "Mid", "2025-01-01T00:00:00+00:00"),
    ]
    assert [r[0] for r in m._sort_rows_newest_first(rows)] == ["new", "mid", "old"]


def test_status_empty_prints_no_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No in-flight worktrees + empty archive → exactly the empty-state hint line."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    rc = m._cmd_status(type("NS", (), {"all": True})())
    assert rc == 0
    assert capsys.readouterr().out == "No changes yet — start one with imp new\n"


def test_status_precondition_error_prints_no_changes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When _control_root() raises PreconditionError (outside a git repo),
    `imp list` still prints the single-line empty message and exits 0."""
    import imp.cli.main as m
    from imp.engine import worktree as _wt

    def _raise() -> Path:
        raise _wt.PreconditionError("not a repo")

    monkeypatch.setattr(m, "_control_root", _raise)

    rc = m._cmd_status(type("NS", (), {"all": True})())
    assert rc == 0
    assert capsys.readouterr().out == "No changes yet — start one with imp new\n"


def test_status_column_alignment_short_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """IDs are always 6 chars (ID_W=6). Verify STATUS column is aligned with
    the header across all data rows."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Long Title That Gets Truncated",
                  quest_id="abc123", base_branch="main")
    Change.create(tmp_path, title="Short", quest_id="xyz789", base_branch="main")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")

    rc = m._cmd_status(type("NS", (), {"all": True})())
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    status_col = lines[0].index("STATUS")
    for data_line in lines[1:]:
        # Slice the STATUS column range and confirm it starts with "new".
        assert data_line[status_col:status_col + len("new")] == "new", (
            f"STATUS column misaligned at offset {status_col}: {data_line!r}"
        )


def test_status_no_ansi_when_NO_COLOR_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With NO_COLOR set, stdout contains no ESC byte."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Gone", quest_id="gone", base_branch="main")
    _complete_and_archive(tmp_path, "gone")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR_FORCE", raising=False)

    rc = m._cmd_status(type("NS", (), {"all": True})())
    assert rc == 0
    out = capsys.readouterr().out
    assert "\x1b[" not in out
    assert "merged" in out


def test_status_all_dims_completed_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With --all and color forced, the completed past section is rendered dim
    (whole-row), so a completed row sits inside a dim SGR span."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Gone", quest_id="gone", base_branch="main")
    _complete_and_archive(tmp_path, "gone")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")

    rc = m._cmd_status(type("NS", (), {"all": True})())
    assert rc == 0
    out = capsys.readouterr().out
    row = next(ln for ln in out.splitlines() if "merged" in ln)
    assert row.startswith("\x1b[2m") and row.endswith("\x1b[0m")


def test_overview_default_shows_merged_disposition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default overview: completed changes appear under `recently merged` with
    disposition `merged` (not the raw `completed`); abandoned are excluded."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Live", quest_id="live", base_branch="main")
    Change.create(tmp_path, title="Done", quest_id="done", base_branch="main")
    Change.create(tmp_path, title="Dead", quest_id="dead", base_branch="main")
    _complete_and_archive(tmp_path, "done")
    _complete_and_archive(tmp_path, "dead", status="abandoned")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")

    rc = m._cmd_status(type("NS", (), {"all": False})(), tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "live" in out
    assert "recently merged" in out
    assert "history" not in out
    assert "done" in out
    assert "merged" in out
    assert "completed" not in out      # disposition is shown, not raw status
    assert "dead" not in out           # abandoned excluded by default
    assert "abandoned" not in out


def test_overview_all_shows_history_label_and_abandoned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """-a: label is `history`; abandoned appears as `abandoned`, completed as
    `merged`."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Done", quest_id="done", base_branch="main")
    Change.create(tmp_path, title="Dead", quest_id="dead", base_branch="main")
    _complete_and_archive(tmp_path, "done")
    _complete_and_archive(tmp_path, "dead", status="abandoned")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")

    rc = m._cmd_status(type("NS", (), {"all": True})(), tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "history" in out
    assert "recently merged" not in out
    assert "done" in out and "merged" in out
    assert "dead" in out and "abandoned" in out


def test_overview_default_caps_at_ten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default keeps at most 10 merged rows; -a lifts the cap."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    for i in range(12):
        cid = f"done{i:02d}"
        Change.create(tmp_path, title=f"Done {i}", quest_id=cid, base_branch="main")
        _complete_and_archive(tmp_path, cid)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")

    m._cmd_status(type("NS", (), {"all": False})(), tmp_path)
    default_out = capsys.readouterr().out
    assert sum(1 for ln in default_out.splitlines() if "done" in ln) == 10

    m._cmd_status(type("NS", (), {"all": True})(), tmp_path)
    all_out = capsys.readouterr().out
    assert sum(1 for ln in all_out.splitlines() if "done" in ln) == 12


def test_overview_excludes_left_conversations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A `left` archived conversation never appears in the history block, with or
    without -a (only completed/abandoned dispositions are rendered)."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Chat", quest_id="chatid", base_branch="main")
    _complete_and_archive(tmp_path, "chatid", status="left")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")

    m._cmd_status(type("NS", (), {"all": False})(), tmp_path)
    assert "chatid" not in capsys.readouterr().out
    m._cmd_status(type("NS", (), {"all": True})(), tmp_path)
    assert "chatid" not in capsys.readouterr().out


def test_overview_empty_history_omits_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Zero merged changes → no blank line, no label."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Live", quest_id="live", base_branch="main")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")

    m._cmd_status(type("NS", (), {"all": False})(), tmp_path)
    out = capsys.readouterr().out
    assert "live" in out
    assert "recently merged" not in out
    assert "history" not in out


def test_overview_only_history_not_reported_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An overview with only merged history is NOT reported as the empty-state hint."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Done", quest_id="done", base_branch="main")
    _complete_and_archive(tmp_path, "done")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")

    m._cmd_status(type("NS", (), {"all": False})(), tmp_path)
    out = capsys.readouterr().out
    assert out != "No changes yet — start one with imp new\n"
    assert "done" in out and "merged" in out


def test_status_all_dims_abandoned_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An abandoned row also lands in the dim past section under --all."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Dead", quest_id="dead", base_branch="main")
    _complete_and_archive(tmp_path, "dead", status="abandoned")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")

    rc = m._cmd_status(type("NS", (), {"all": True})())
    assert rc == 0
    out = capsys.readouterr().out
    row = next(ln for ln in out.splitlines() if "abandoned" in ln)
    assert row.startswith("\x1b[2m") and row.endswith("\x1b[0m")


def test_status_default_inflight_columns_and_color(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default view: header leads with ID, then TITLE, STATUS with color."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Add version flag",
                  quest_id="yaitd7", base_branch="main")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")

    rc = m._cmd_status(type("NS", (), {"all": False})())
    assert rc == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    plain_header = re.sub(r"\x1b\[[0-9;]*m", "", lines[0])
    assert plain_header.startswith("ID")
    assert "NAME" in plain_header
    assert "STATUS" in plain_header
    assert "Add version flag" in out
    assert "\x1b[36mnew\x1b[0m" in out
    # id leads the row
    assert lines[1].startswith("yaitd7")


def test_abandon_archives_runtime_and_retains_wip(tmp_path: Path) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Doomed", quest_id="doomed",
                       base_branch="main")
    art = ch.quest_dir / "artifacts" / "plans"
    art.mkdir(parents=True)
    (art / "doomed-plan-1.md").write_text("# plan\n")
    worktree.artifact_commit(
        tmp_path, "doomed",
        {"plans/doomed-plan-1.md": art / "doomed-plan-1.md"},
        message="artifact: plan")

    rc = m._abandon(tmp_path, "doomed")
    assert rc == 0
    from imp.engine.archive import read as archive_read
    # Artifact stays reachable on the RETAINED wip ref (spec AC 3).
    assert archive_read.read_artifact(tmp_path, "doomed", "plans/doomed-plan-1.md") \
        == b"# plan\n"
    # Runtime snapshot is archived (record/blob/index), not journaled.
    assert archive_read.read_state_json(tmp_path, "doomed") is not None
    assert subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "refs/imp/artifacts/doomed"],
        cwd=tmp_path, capture_output=True).returncode == 0


def test_abandon_without_wip_still_archives_runtime(tmp_path: Path) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Bare", quest_id="bare",
                  base_branch="main")
    # No artifact committed → no wip ref exists.
    rc = m._abandon(tmp_path, "bare")
    assert rc == 0
    from imp.engine.archive import read as archive_read
    # Runtime state is archived even without any artifact; no wip ref was ever created.
    assert archive_read.read_state_json(tmp_path, "bare") is not None
    assert subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "refs/imp/artifacts/bare"],
        cwd=tmp_path, capture_output=True).returncode != 0


def test_show_events_and_status_from_archive(tmp_path, monkeypatch) -> None:
    import imp.cli.main as m

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)

    events = tmp_path / "e.jsonl"
    events.write_text(
        '{"id":"a-e0","parent_id":null,"agent_id":null,'
        '"timestamp":"2026-06-01T00:00:01+00:00","type":"change_created",'
        '"payload":{"id":"arch01"}}\n')
    cj = tmp_path / "c.json"
    cj.write_text(json.dumps({
        "id": "arch01", "title": "Arch", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    sj = tmp_path / "s.json"
    sj.write_text(json.dumps({"id": "arch01", "status": "completed", "step": None}))
    archive.archive_quest(
        tmp_path, "arch01", events_path=events,
        quest_json_path=cj, state_json_path=sj)

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    def _run(*argv: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert m.main(list(argv)) == 0, buf.getvalue()
        return buf.getvalue()

    assert "change_created" in _run("show", "arch01", "events")
    assert "completed" in _run("show", "arch01", "status")


def test_finalize_archives_runtime_and_retains_wip(tmp_path: Path) -> None:
    from imp.engine import executor
    from imp.engine.state import JsonlEventLog

    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Doc Thing", quest_id="doc-thing",
                       base_branch="main")
    # An artifact accumulated on the wip ref during the run.
    art = ch.quest_dir / "artifacts" / "spec"
    art.mkdir(parents=True)
    (art / "doc-thing.md").write_text("# the spec\n")
    files = {str(p.relative_to(ch.quest_dir / "artifacts")): p
             for p in (art / "doc-thing.md",)}
    worktree.artifact_commit(tmp_path, "doc-thing", files, message="artifact: spec")
    # Simulate a completed merge so _complete takes the finalize path.
    st = State.load(ch.state_dir)
    st.merge_commit = "0" * 40
    st.save(ch.state_dir)

    executor._complete(ch, State.load(ch.state_dir), JsonlEventLog(ch.events_path))

    from imp.engine.archive import read as archive_read
    # The artifact stays on the RETAINED wip ref; the runtime snapshot is archived.
    assert archive_read.read_artifact(tmp_path, "doc-thing", "spec/doc-thing.md") \
        == b"# the spec\n"
    assert archive_read.read_events(tmp_path, "doc-thing") is not None
    assert archive_read.read_quest_json(tmp_path, "doc-thing") is not None
    assert subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet",
         "refs/imp/artifacts/doc-thing"],
        cwd=tmp_path, capture_output=True).returncode == 0


def test_log_reads_events_from_archive_blob(tmp_path: Path, monkeypatch) -> None:
    import imp.cli.main as m

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)

    events = tmp_path / "e.jsonl"
    events.write_text(
        '{"id":"log-e0","parent_id":null,"agent_id":null,'
        '"timestamp":"2026-06-01T00:00:01+00:00","type":"change_created",'
        '"payload":{"id":"logtest"}}\n'
        '{"id":"log-e1","parent_id":"log-e0","agent_id":"plan-writer",'
        '"timestamp":"2026-06-01T00:00:09+00:00","type":"step_started",'
        '"payload":{"step":"plan-writer","phase":"plan"}}\n'
    )
    cj = tmp_path / "c.json"
    cj.write_text(json.dumps({
        "id": "logtest", "title": "Log Test", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00", "depends_on": []}))
    sj = tmp_path / "s.json"
    sj.write_text(json.dumps({"id": "logtest", "status": "completed", "step": None}))
    archive.archive_quest(
        tmp_path, "logtest", events_path=events,
        quest_json_path=cj, state_json_path=sj)

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = m.main(["log", "logtest"])
    out = buf.getvalue()
    assert rc == 0, out
    assert "plan-writer" in out    # rendered from the archive events blob


def test_quest_meta_resolves_archived_from_archive(tmp_path: Path) -> None:
    """After abandon (permanent change home rmtree'd, worktree gone), _quest_meta
    resolves the Change's metadata from the archive record, not the retired journal."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Meta One", quest_id="meta-one",
                  base_branch="main")
    home = tmp_path / ".imp" / "quests" / "meta-one"
    assert (home / "quest.json").exists()
    _complete_and_archive(tmp_path, "meta-one")
    shutil.rmtree(home)                       # abandon path: permanent home gone
    assert not home.exists()
    assert not (tmp_path / ".imp" / "worktrees" / "meta-one").exists()

    meta = m._quest_meta(tmp_path, "meta-one")
    assert meta is not None
    assert meta["base_branch"] == "main"
    assert meta["title"] == "Meta One"


def test_status_all_lists_archived(tmp_path, monkeypatch) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)

    # An archived (index) completed Change with no in-flight home.
    events = tmp_path / "e.jsonl"
    events.write_text(
        '{"id":"x-e0","timestamp":"2026-06-02T00:00:00+00:00",'
        '"type":"change_created","payload":{}}\n')
    cj = tmp_path / "c.json"
    cj.write_text(json.dumps({
        "id": "arch01", "title": "Archived One", "base_branch": "main",
        "created_at": "2026-06-02T00:00:00+00:00", "depends_on": []}))
    sj = tmp_path / "s.json"
    sj.write_text(json.dumps({"id": "arch01", "status": "completed", "step": None}))
    archive.archive_quest(
        tmp_path, "arch01", events_path=events,
        quest_json_path=cj, state_json_path=sj)

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert m.main(["list", "--all"]) == 0
    out = buf.getvalue()
    assert "arch01" in out
    assert "Archived One" in out
