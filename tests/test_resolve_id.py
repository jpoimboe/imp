"""_resolve_id: a token resolves iff it is an unambiguous substring of exactly
one change's id (spec AC 7)."""

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
    _mk(tmp_path, "xyz789", "")
    assert _resolve_id(tmp_path, "xyz789") == "xyz789"


def test_unique_partial_id_resolves(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _mk(tmp_path, "abc123", "Add OAuth")
    assert _resolve_id(tmp_path, "bc12") == "abc123"


def test_title_substring_does_not_resolve(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _mk(tmp_path, "abc123", "Add OAuth")
    with pytest.raises(SystemExit):
        _resolve_id(tmp_path, "oauth")


def test_ambiguous_id_substring_lists_candidates(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    _mk(tmp_path, "abc123", "Add OAuth")
    _mk(tmp_path, "abc999", "Add login")
    with pytest.raises(SystemExit):
        _resolve_id(tmp_path, "abc")
    err = capsys.readouterr().err
    assert "ambiguous" in err
    assert "abc123" in err and "abc999" in err
    assert "ID" in err


def test_no_match_exits(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _mk(tmp_path, "abc123", "One")
    with pytest.raises(SystemExit):
        _resolve_id(tmp_path, "zzzzzz")


@pytest.mark.parametrize("token", ["", "  "])
def test_empty_token_does_not_resolve_sole_change(tmp_path: Path, token: str) -> None:
    _seed_control(tmp_path)
    _mk(tmp_path, "abc123", "One")  # exactly one live change
    with pytest.raises(SystemExit) as exc:
        _resolve_id(tmp_path, token)
    assert exc.value.code != 0
