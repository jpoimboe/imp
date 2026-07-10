"""`_emit_merge_blocked` appends git's own stderr (the `merge_blocked` event's
`detail`) for the `refused` and `conflict` buckets, so the surfaced reason is
truthful rather than a generic template. REFUSED in particular is a neutral
bucket (overwrite, unrelated histories, ref-lock, object-store corruption) — the
template alone would mislead for every refusal that isn't an overwrite."""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.cli import main as cli_main
from imp.engine.quest import Quest
from tests.conftest import _seed_control


def _change(tmp_path: Path) -> Quest:
    _seed_control(tmp_path)
    return Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")


def test_refused_surfaces_git_detail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    ch = _change(tmp_path)
    cli_main._emit_merge_blocked(
        ch, "refused", "fatal: refusing to merge unrelated histories")
    out = capsys.readouterr().out
    assert "fatal: refusing to merge unrelated histories" in out


def test_conflict_surfaces_git_detail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    ch = _change(tmp_path)
    cli_main._emit_merge_blocked(
        ch, "conflict", "CONFLICT (content): Merge conflict in f.txt")
    out = capsys.readouterr().out
    assert "CONFLICT (content): Merge conflict in f.txt" in out
    # The manual-merge remediation is still present (detail augments, not replaces).
    assert "manually" in out


def test_worktree_dirty_ignores_detail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """worktree_dirty has a precise, actionable template; raw git noise would only
    distract, so detail is not appended for it."""
    ch = _change(tmp_path)
    cli_main._emit_merge_blocked(ch, "worktree_dirty", "some git noise")
    out = capsys.readouterr().out
    assert "some git noise" not in out
    assert "Uncommitted work" in out


def test_refused_without_detail_is_safe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A blocking outcome with no captured stderr (detail=None) must still print a
    clean message with no dangling 'git said:' line."""
    ch = _change(tmp_path)
    cli_main._emit_merge_blocked(ch, "refused", None)
    out = capsys.readouterr().out
    assert "git said" not in out
    assert "imp resume" in out


def test_exhaustion_note_renders_ahead_of_detail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    ch = _change(tmp_path)
    note = "imp attempted 3 recovery passes and the merge still failed."
    cli_main._emit_merge_blocked(
        ch, "conflict", "CONFLICT (content): Merge conflict in f.txt", note)
    out = capsys.readouterr().out
    assert note in out
    assert "CONFLICT (content): Merge conflict in f.txt" in out
    assert out.index(note) < out.index("git said")   # note precedes git's detail


def test_no_note_leaves_output_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    ch = _change(tmp_path)
    cli_main._emit_merge_blocked(ch, "conflict", "boom")
    out = capsys.readouterr().out
    assert "recovery passes" not in out
    assert "boom" in out
