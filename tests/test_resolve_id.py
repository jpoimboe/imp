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
    _mk(tmp_path, "aaa111", "One")
    assert _resolve_id(tmp_path, "aaa111") == "aaa111"


def test_unique_suffix(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _mk(tmp_path, "aaa111", "One")
    _mk(tmp_path, "bbb222", "Two")
    assert _resolve_id(tmp_path, "222") == "bbb222"


def test_shared_prefix_is_ambiguous(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    _mk(tmp_path, "aaa111", "One")
    _mk(tmp_path, "aaa222", "Two")
    with pytest.raises(SystemExit):
        _resolve_id(tmp_path, "aaa")     # prefix shared -> ambiguous, not resolved
    err = capsys.readouterr().err
    # AC 5: rejected as ambiguous AND lists the candidates via the shared formatter
    # (TITLE header + each title + resume token), not a blank no-match.
    assert "ambiguous" in err
    assert "TITLE" in err
    assert "One" in err and "Two" in err
    assert "aaa111" in err and "aaa222" in err


def test_current_handle_resolves(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = _mk(tmp_path, "aaa111", "Add OAuth")
    assert ch.handle == "add-oauth-aaa111"
    assert _resolve_id(tmp_path, "add-oauth") == "aaa111"


def test_bare_name_resolves(tmp_path: Path) -> None:
    # The bare name a user naturally types resolves via the slug tier.
    _seed_control(tmp_path)
    _mk(tmp_path, "aaa111", "Add version flag")
    assert _resolve_id(tmp_path, "add-version-flag") == "aaa111"


def test_bare_name_collision_is_ambiguous(tmp_path: Path, capsys) -> None:
    # Two changes sharing a name -> the bare name is ambiguous and lists
    # candidates, rather than a blank no-match.
    _seed_control(tmp_path)
    _mk(tmp_path, "aaa111", "Add version flag")
    _mk(tmp_path, "bbb222", "Add version flag")
    with pytest.raises(SystemExit):
        _resolve_id(tmp_path, "add-version-flag")
    err = capsys.readouterr().err
    assert "ambiguous" in err
    assert "aaa111" in err and "bbb222" in err


def test_no_match_exits(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _mk(tmp_path, "aaa111", "One")
    with pytest.raises(SystemExit):
        _resolve_id(tmp_path, "nonexistent")


def test_shared_suffix_is_ambiguous(tmp_path: Path, capsys) -> None:
    _seed_control(tmp_path)
    _mk(tmp_path, "aaa111", "One")
    _mk(tmp_path, "bbb111", "Two")
    with pytest.raises(SystemExit):
        _resolve_id(tmp_path, "111")        # suffix shared by both ids -> ambiguous
    err = capsys.readouterr().err
    assert "ambiguous" in err
    assert "One" in err and "Two" in err
    assert "aaa111" in err and "bbb111" in err
