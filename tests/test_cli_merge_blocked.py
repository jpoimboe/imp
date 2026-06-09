"""`_emit_merge_blocked` appends git's own stderr (the `merge_blocked` event's
`detail`) for the `refused` and `conflict` buckets, so the surfaced reason is
truthful rather than a generic template. REFUSED in particular is a neutral
bucket (overwrite, unrelated histories, ref-lock, object-store corruption) — the
template alone would mislead for every refusal that isn't an overwrite."""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.cli import main as cli_main
from imp.engine.change import Change
from tests.conftest import _seed_control


def _change(tmp_path: Path) -> Change:
    _seed_control(tmp_path)
    return Change.create(tmp_path, title="x", change_id="feat", base_branch="main")


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
