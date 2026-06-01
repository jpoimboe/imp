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
        rc = main(["log", "--no-pager", "done-thing"])
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

    rc = main(["log", "--no-pager", "ghost"])
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
        rc = main(["log", "--no-pager"])  # no slug, no in-flight worktrees
    assert rc == 0
    assert "Only One" in buf.getvalue()
