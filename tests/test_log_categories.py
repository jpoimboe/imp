"""imp log --show/--hide compose category toggles over the lean base."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from imp.cli import render_text
from imp.cli.main import _log_show_set, main
from imp.cli.render_text import LOG_CATEGORIES
from imp.engine.quest import Quest
from tests.conftest import _seed_control

_ID = "logq01"
_ALL = frozenset(LOG_CATEGORIES)


def test_log_show_set_default_is_lean() -> None:
    assert _log_show_set(False, [], []) == frozenset()


def test_log_show_set_full_is_all() -> None:
    assert _log_show_set(True, [], []) == _ALL


def test_log_show_set_show_adds_over_lean() -> None:
    assert _log_show_set(False, ["thinking"], []) == frozenset({"thinking"})


def test_log_show_set_hide_removes_from_full() -> None:
    assert _log_show_set(True, [], ["tiers"]) == _ALL - {"tiers"}


def test_log_show_set_show_and_hide_compose() -> None:
    assert _log_show_set(False, ["thinking", "tools"], ["tools"]) == frozenset(
        {"thinking"})


def test_log_unknown_category_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["log", _ID, "--show", "bogus"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown category 'bogus'" in err


def test_log_follow_show_conflict(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["log", _ID, "--follow", "--show", "thinking"])
    assert rc == 2
    assert "not allowed with argument --show/--hide" in capsys.readouterr().err


def test_log_follow_hide_conflict(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["log", _ID, "--follow", "--hide", "tiers"])
    assert rc == 2
    assert "not allowed with argument --show/--hide" in capsys.readouterr().err


def test_log_threads_computed_show_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import imp.cli.main as m

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Logq", quest_id=_ID, base_branch="main")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    captured: dict[str, object] = {}

    def _fake_render_tree(tree, **kwargs):
        captured.update(kwargs)
        return iter(["(rendered)"])

    monkeypatch.setattr(render_text, "render_tree", _fake_render_tree)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["log", _ID, "--full", "--hide", "tiers"])
    assert rc == 0
    assert captured["show"] == _ALL - {"tiers"}
