"""imp log / imp status against a journaled (merged, worktree-removed) Change.

Mirrors executor._complete's journal-then-remove sequence by hand, then drives
the CLI. No live providers needed.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from imp.cli.main import main
from imp.engine import worktree
from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _seed_control


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
    Change.create(tmp_path, title="Done Thing", slug="done-thing", base_branch="main")
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
    assert "Done Thing" in buf.getvalue()


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
    Change.create(tmp_path, title="Only One", slug="only-one", base_branch="main")
    _complete_and_journal(tmp_path, "only-one")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["log"])  # no slug, no in-flight worktrees
    assert rc == 0
    assert "Only One" in buf.getvalue()


def test_status_shows_completed_section_from_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    # One in-flight, one merged-and-journaled.
    Change.create(tmp_path, title="Live One", slug="live-one", base_branch="main")
    Change.create(tmp_path, title="Gone One", slug="gone-one", base_branch="main")
    _complete_and_journal(tmp_path, "gone-one")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    rc = m._cmd_status(type("NS", (), {})())
    assert rc == 0
    out = capsys.readouterr().out
    assert "In-flight Changes" in out
    assert "Completed Changes" in out
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
    ch = Change.create(tmp_path, title="Both", slug="both", base_branch="main")
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
    rc = m._cmd_status(type("NS", (), {})())
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
    Change.create(tmp_path, title="Good", slug="good", base_branch="main")
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
    rc = m._cmd_status(type("NS", (), {})())
    assert rc == 0
    out = capsys.readouterr().out
    assert "good" in out
    assert "broken" not in out
