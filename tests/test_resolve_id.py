"""_resolve_id precedence: exact id / unique suffix / current slug / ambiguous
(spec §5, AC 5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.cli.main import _resolve_id
from imp.engine.change import Change
from tests.conftest import _seed_control


def _mk(tmp_path: Path, change_id: str, title: str) -> Change:
    return Change.create(tmp_path, title=title, change_id=change_id, base_branch="main")


def test_exact_id(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _mk(tmp_path, "2026-06-04-aaaaaa", "One")
    assert _resolve_id(tmp_path, "2026-06-04-aaaaaa") == "2026-06-04-aaaaaa"


def test_unique_suffix(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _mk(tmp_path, "2026-06-04-aaaaaa", "One")
    _mk(tmp_path, "2026-06-04-bbbbbb", "Two")
    assert _resolve_id(tmp_path, "bbbbbb") == "2026-06-04-bbbbbb"


def test_shared_date_prefix_is_ambiguous(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    _mk(tmp_path, "2026-06-04-aaaaaa", "One")
    _mk(tmp_path, "2026-06-04-bbbbbb", "Two")
    with pytest.raises(SystemExit):
        _resolve_id(tmp_path, "2026-06-04")     # prefix shared -> ambiguous, not resolved
    err = capsys.readouterr().err
    # AC 5: rejected as ambiguous AND lists the candidates (not a blank no-match).
    assert "ambiguous" in err
    assert "2026-06-04-aaaaaa" in err and "2026-06-04-bbbbbb" in err


def test_current_slug_resolves(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _mk(tmp_path, "2026-06-04-aaaaaa", "Add OAuth")
    assert ch.slug == "2026-06-04-add-oauth"
    assert _resolve_id(tmp_path, "2026-06-04-add-oauth") == "2026-06-04-aaaaaa"


def test_no_match_exits(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _mk(tmp_path, "2026-06-04-aaaaaa", "One")
    with pytest.raises(SystemExit):
        _resolve_id(tmp_path, "nonexistent")


def test_cross_day_shared_suffix_is_ambiguous(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    _mk(tmp_path, "2026-06-04-zzzzzz", "One")
    _mk(tmp_path, "2026-06-05-zzzzzz", "Two")
    with pytest.raises(SystemExit):
        _resolve_id(tmp_path, "zzzzzz")        # suffix shared across days -> ambiguous
    err = capsys.readouterr().err
    assert "2026-06-04-zzzzzz" in err and "2026-06-05-zzzzzz" in err
