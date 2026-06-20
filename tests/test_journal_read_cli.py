"""imp log / imp status against a journaled (merged, worktree-removed) Change.

Mirrors executor._complete's journal-then-remove sequence by hand, then drives
the CLI. No live providers needed.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from imp.cli.main import main
from imp.engine import journal, worktree
from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _seed_control

# A phase that renders through the narrative — the proof the journal was read.
# (change_created emits no line now that the slug/title banner is gone, so we
# need a real rendered node to assert on.)
_JOURNAL_MARKER_PHASE = "summary"


def _seed_marker_phase(control: Path, slug: str) -> None:
    """Append a step_started event so the change has a node that renders through
    the narrative — change_created emits no line now that the slug/title banner
    is gone, so a log test needs a real rendered marker to assert on. Reuses the
    change's own first-event timestamp so it never perturbs last-activity sorting.
    Call BEFORE _complete_and_journal."""
    ch = Change.load(control, slug)
    with ch.events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "id": "marker", "agent_id": None,
            "timestamp": "2026-05-27T10:00:01+00:00",
            "type": "step_started",
            "payload": {"step": "s", "kind": "agent", "phase": _JOURNAL_MARKER_PHASE,
                        "model": "m", "provider": "anthropic"},
        }) + "\n")


def _complete_and_journal(control: Path, slug: str) -> None:
    """Flip the Change to completed, journal the three files, remove the
    worktree — the exact set executor._complete writes on the merge path."""
    ch = Change.load(control, slug)
    st = State.load(ch.state_dir)
    st.status = "completed"
    st.save(ch.state_dir)
    worktree.journal_commit(control, slug, {
        "events.jsonl": ch.events_path,
        "change.json": ch.change_dir / "change.json",
        "state.json": ch.state_dir / "state.json",
    })
    worktree.remove_worktree(control, slug)


def test_log_falls_back_to_journal_after_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Done Thing", change_id="done-thing", base_branch="main")
    _seed_marker_phase(tmp_path, "done-thing")
    _complete_and_journal(tmp_path, "done-thing")
    # Worktree is gone: Change.load now raises; log must use the journal.
    assert not (tmp_path / ".imp" / "worktrees" / "done-thing").exists()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["log", "done-thing"])
    assert rc == 0
    assert _JOURNAL_MARKER_PHASE in buf.getvalue()


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


def test_log_no_slug_falls_through_to_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Only One", change_id="only-one", base_branch="main")
    _seed_marker_phase(tmp_path, "only-one")
    _complete_and_journal(tmp_path, "only-one")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["log"])  # no slug, no in-flight worktrees
    assert rc == 0
    assert _JOURNAL_MARKER_PHASE in buf.getvalue()


def test_status_shows_completed_section_from_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    # One in-flight, one merged-and-journaled.
    Change.create(tmp_path, title="Live One", change_id="live-one", base_branch="main")
    Change.create(tmp_path, title="Gone One", change_id="gone-one", base_branch="main")
    _complete_and_journal(tmp_path, "gone-one")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    rc = m._cmd_status(type("NS", (), {"all": True})())
    assert rc == 0
    out = capsys.readouterr().out
    # in-flight slug appears under in-flight; merged slug under completed.
    assert "live-one" in out
    assert "gone-one" in out
    # The merged Change shows its title + completed status, not blank columns.
    assert "Gone One" in out
    assert "completed" in out


def test_status_inflight_takes_precedence_over_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If a slug is BOTH a live worktree and in the journal (crash between
    journal and remove), the completed section must not double-list it."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Both", change_id="both", base_branch="main")
    # Journal it but DON'T remove the worktree (simulates the crash window).
    worktree.journal_commit(tmp_path, "both", {
        "events.jsonl": ch.events_path,
        "change.json": ch.change_dir / "change.json",
        "state.json": ch.state_dir / "state.json",
    })

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


def test_status_skips_malformed_journal_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A corrupt journaled state.json (valid otherwise-unreachable on-disk
    corruption) must be skipped, not crash the whole `imp status` listing."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Good", change_id="good", base_branch="main")
    _complete_and_journal(tmp_path, "good")
    # A second journaled slug whose state.json is not valid JSON.
    bad = tmp_path / ".imp" / "bad-state.json"
    bad.write_text("{ not json")
    good_change = tmp_path / ".imp" / "bad-change.json"
    good_change.write_text('{"slug":"broken","title":"Broken"}')
    worktree.journal_commit(tmp_path, "broken", {
        "state.json": bad,
        "change.json": good_change,
    })

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    completed = m._completed_rows(tmp_path)
    assert [r[0] for r in completed] == ["good"]  # broken entry skipped
    rc = m._cmd_status(type("NS", (), {"all": True})())
    assert rc == 0
    out = capsys.readouterr().out
    assert "good" in out
    assert "broken" not in out


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
    """No in-flight worktrees + empty journal → exactly `No changes.\\n`."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    rc = m._cmd_status(type("NS", (), {"all": True})())
    assert rc == 0
    assert capsys.readouterr().out == "No changes.\n"


def test_status_precondition_error_prints_no_changes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When _control_root() raises PreconditionError (outside a git repo),
    `imp status` still prints the single-line empty message and exits 0."""
    import imp.cli.main as m
    from imp.engine import worktree as _wt

    def _raise() -> Path:
        raise _wt.PreconditionError("not a repo")

    monkeypatch.setattr(m, "_control_root", _raise)

    rc = m._cmd_status(type("NS", (), {"all": True})())
    assert rc == 0
    assert capsys.readouterr().out == "No changes.\n"


def test_status_column_alignment_short_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """IDs are always 6 chars (ID_W=6). Verify STATUS column is aligned with
    the header across all data rows."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Long Title That Gets Truncated",
                  change_id="abc123", base_branch="main")
    Change.create(tmp_path, title="Short", change_id="xyz789", base_branch="main")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")

    rc = m._cmd_status(type("NS", (), {"all": True})())
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    status_col = lines[0].index("STATUS")
    for data_line in lines[1:]:
        # Slice the STATUS column range and confirm it starts with "running".
        assert data_line[status_col:status_col + len("running")] == "running", (
            f"STATUS column misaligned at offset {status_col}: {data_line!r}"
        )


def test_status_no_ansi_when_NO_COLOR_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With NO_COLOR set, stdout contains no ESC byte."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Gone", change_id="gone", base_branch="main")
    _complete_and_journal(tmp_path, "gone")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR_FORCE", raising=False)

    rc = m._cmd_status(type("NS", (), {"all": True})())
    assert rc == 0
    out = capsys.readouterr().out
    assert "\x1b[" not in out
    assert "completed" in out


def test_status_all_dims_completed_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With --all and color forced, the completed past section is rendered dim
    (whole-row), so a completed row sits inside a dim SGR span."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Gone", change_id="gone", base_branch="main")
    _complete_and_journal(tmp_path, "gone")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")

    rc = m._cmd_status(type("NS", (), {"all": True})())
    assert rc == 0
    out = capsys.readouterr().out
    row = next(ln for ln in out.splitlines() if "completed" in ln)
    assert row.startswith("\x1b[2m") and row.endswith("\x1b[0m")


def _abandon_and_journal(control: Path, slug: str) -> None:
    """Flip the Change to abandoned + journal it, the way _abandon does (minus
    the worktree teardown, which we trigger via remove_worktree below)."""
    ch = Change.load(control, slug)
    st = State.load(ch.state_dir)
    st.status = "abandoned"
    st.save(ch.state_dir)
    worktree.journal_commit(control, slug, {
        "events.jsonl": ch.events_path,
        "change.json": ch.change_dir / "change.json",
        "state.json": ch.state_dir / "state.json",
    })
    worktree.remove_worktree(control, slug)


def test_status_hides_completed_and_abandoned_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without --all, only in-flight Changes list; completed/abandoned Changes
    (journal-backed history) are suppressed."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Live", change_id="live", base_branch="main")
    Change.create(tmp_path, title="Done", change_id="done", base_branch="main")
    Change.create(tmp_path, title="Dead", change_id="dead", base_branch="main")
    _complete_and_journal(tmp_path, "done")
    _abandon_and_journal(tmp_path, "dead")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")

    rc = m._cmd_status(type("NS", (), {"all": False})())
    assert rc == 0
    out = capsys.readouterr().out
    assert "live" in out
    assert "done" not in out
    assert "dead" not in out
    assert "completed" not in out
    assert "abandoned" not in out


def test_status_all_dims_abandoned_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An abandoned row also lands in the dim past section under --all."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Dead", change_id="dead", base_branch="main")
    _abandon_and_journal(tmp_path, "dead")

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
                  change_id="yaitd7", base_branch="main")
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
    assert "\x1b[32mrunning\x1b[0m" in out
    # id leads the row
    assert lines[1].startswith("yaitd7")


def test_abandon_archives_runtime_and_retains_wip(tmp_path: Path) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Doomed", change_id="doomed",
                       base_branch="main")
    art = ch.change_dir / "artifacts" / "plans"
    art.mkdir(parents=True)
    (art / "doomed-plan-1.md").write_text("# plan\n")
    worktree.artifact_commit(
        tmp_path, "doomed",
        {"plans/doomed-plan-1.md": art / "doomed-plan-1.md"},
        message="artifact: plan")

    rc = m._abandon(tmp_path, "doomed", assume_yes=True)
    assert rc == 0
    from imp.engine.archive import read as archive_read
    # Artifact stays reachable on the RETAINED wip ref (spec AC 3).
    assert archive_read.read_artifact(tmp_path, "doomed", "plans/doomed-plan-1.md") \
        == b"# plan\n"
    # Runtime snapshot is archived (record/blob/index), not journaled.
    assert archive_read.read_state_json(tmp_path, "doomed") is not None
    assert subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "refs/imp/journal-wip/doomed"],
        cwd=tmp_path, capture_output=True).returncode == 0


def test_abandon_without_wip_still_archives_runtime(tmp_path: Path) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Bare", change_id="bare",
                  base_branch="main")
    # No artifact committed → no wip ref exists.
    rc = m._abandon(tmp_path, "bare", assume_yes=True)
    assert rc == 0
    from imp.engine.archive import read as archive_read
    # Runtime state is archived even without any artifact; no wip ref was ever created.
    assert archive_read.read_state_json(tmp_path, "bare") is not None
    assert subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "refs/imp/journal-wip/bare"],
        cwd=tmp_path, capture_output=True).returncode != 0


def test_finalize_archives_runtime_and_retains_wip(tmp_path: Path) -> None:
    from imp.engine import executor
    from imp.engine.state import JsonlEventLog

    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Doc Thing", change_id="doc-thing",
                       base_branch="main")
    # An artifact accumulated on the wip ref during the run.
    art = ch.change_dir / "artifacts" / "spec"
    art.mkdir(parents=True)
    (art / "doc-thing.md").write_text("# the spec\n")
    files = {str(p.relative_to(ch.change_dir / "artifacts")): p
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
    assert archive_read.read_change_json(tmp_path, "doc-thing") is not None
    assert subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet",
         "refs/imp/journal-wip/doc-thing"],
        cwd=tmp_path, capture_output=True).returncode == 0
