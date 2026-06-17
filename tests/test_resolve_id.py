"""_resolve_id: a token resolves iff it is an unambiguous substring of exactly
one change's live handle (spec AC 7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.cli.main import _resolve_id
from imp.engine.change import Change
from tests.conftest import _seed_control


def _mk(tmp_path: Path, change_id: str, title: str) -> Change:
    return Change.create(tmp_path, title=title, change_id=change_id, base_branch="main")


def test_bare_id_resolves_named_or_unnamed(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _mk(tmp_path, "abc123", "Add OAuth")
    assert _resolve_id(tmp_path, "abc123") == "abc123"
    # unnamed: derive_handle("", "xyz789") == "xyz789", bare id still resolves
    _mk(tmp_path, "xyz789", "")
    assert _resolve_id(tmp_path, "xyz789") == "xyz789"


def test_name_substring_resolves(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _mk(tmp_path, "abc123", "Add OAuth")        # handle add-oauth-abc123
    assert _resolve_id(tmp_path, "oauth") == "abc123"


def test_full_handle_resolves(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _mk(tmp_path, "abc123", "Add OAuth")
    assert _resolve_id(tmp_path, "add-oauth-abc123") == "abc123"


def test_ambiguous_substring_lists_candidates(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    _mk(tmp_path, "abc123", "Add OAuth")        # add-oauth-abc123
    _mk(tmp_path, "def456", "Add OAuth login")  # add-oauth-login-def456
    with pytest.raises(SystemExit):
        _resolve_id(tmp_path, "oauth")
    err = capsys.readouterr().err
    assert "ambiguous" in err
    assert "abc123" in err and "def456" in err
    assert "HANDLE" in err


def test_no_match_exits(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _mk(tmp_path, "abc123", "One")
    with pytest.raises(SystemExit):
        _resolve_id(tmp_path, "zzzzzz")
