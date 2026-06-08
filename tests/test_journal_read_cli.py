"""imp log / imp status against a journaled (merged, worktree-removed) Change.

Mirrors executor._complete's journal-then-remove sequence by hand, then drives
the CLI. No live providers needed.
"""

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
    assert out.count("both") == 1  # only in the in-flight section


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


def _set_created_at(control: Path, slug: str, created_at: str) -> None:
    """Overwrite change.json's created_at before journaling so the row's sort
    key is deterministic (Change.create stamps wall-clock now())."""
    meta_path = (control / ".imp" / "worktrees" / slug
                 / ".imp" / "change" / "change.json")
    meta = json.loads(meta_path.read_text())
    meta["created_at"] = created_at
    meta_path.write_text(json.dumps(meta))


def test_status_sorts_rows_by_creation_date_oldest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rows sort by Change creation date, oldest first — independent of
    last_activity (the 'middle' Change is journaled last but created in the
    middle, so it must land between the others, not at an end)."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    Change.create(tmp_path, title="Oldest", change_id="oldest", base_branch="main")
    Change.create(tmp_path, title="Middle", change_id="middle", base_branch="main")
    Change.create(tmp_path, title="Newest", change_id="newest", base_branch="main")
    _set_created_at(tmp_path, "oldest", "2024-01-01T00:00:00+00:00")
    _set_created_at(tmp_path, "middle", "2025-01-01T00:00:00+00:00")
    _set_created_at(tmp_path, "newest", "2026-01-01T00:00:00+00:00")

    # Journal in an order unrelated to creation date to prove the sort key is
    # created_at, not journaling/activity order.
    _complete_and_journal(tmp_path, "newest")
    _complete_and_journal(tmp_path, "oldest")
    _complete_and_journal(tmp_path, "middle")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")

    rc = m._cmd_status(type("NS", (), {"all": True})())
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    # Header is lines[0]; data rows follow. First whitespace-delimited token
    # of each data row is the slug.
    slugs_in_order = [line.split()[0] for line in lines[1:]]
    assert slugs_in_order == ["oldest", "middle", "newest"]


def test_status_empty_prints_no_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No in-flight worktrees + empty journal → exactly `No Changes.\\n`."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    rc = m._cmd_status(type("NS", (), {"all": True})())
    assert rc == 0
    assert capsys.readouterr().out == "No Changes.\n"


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
    assert capsys.readouterr().out == "No Changes.\n"


def test_status_column_alignment_under_long_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A 28-char slug widens SLUG without shifting STATUS off the header's
    STATUS column position in any data row."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    long_slug = "2026-06-04-kelp-hazel-heath0"  # 28 chars
    assert len(long_slug) == 28
    Change.create(tmp_path, title="Long Slug", change_id=long_slug, base_branch="main")
    Change.create(tmp_path, title="Short",     change_id="short",   base_branch="main")

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


def test_status_colors_completed_with_gray_when_FORCE_COLOR(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With FORCE_COLOR set, a completed row's STATUS cell is wrapped in the
    gray SGR sequence around the literal text 'completed'."""
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
    # palette._ANSI["gray"] is "\x1b[90m"; reset is "\x1b[0m".
    assert "\x1b[90mcompleted\x1b[0m" in out


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


def test_status_all_shows_abandoned_in_gray(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With --all and color forced, an abandoned row's STATUS cell is wrapped in
    the same gray SGR sequence as completed."""
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
    assert "\x1b[90mabandoned\x1b[0m" in out
