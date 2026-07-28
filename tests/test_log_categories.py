"""imp log --show/--hide compose category toggles over the lean base."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock

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
    assert "imp: unknown category 'bogus'" in err   # canonical imp: prefix
    assert "imp log:" not in err                     # divergent prefix gone


def _follow_driver_kwargs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str],
) -> dict[str, object]:
    """Run `imp log -f …` against a seeded quest (a real live home, so the
    tailing route is taken) and return the kwargs the follow path passed to
    LiveDriver. anyio.run is stubbed so the poll loop never starts."""
    import imp.cli.main as m

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Logq", quest_id=_ID, base_branch="main")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    seen: dict[str, object] = {}

    def _fake_driver(**kwargs: object) -> MagicMock:
        seen.update(kwargs)
        return MagicMock()

    monkeypatch.setattr("imp.cli.live_driver.LiveDriver", _fake_driver)
    monkeypatch.setattr("anyio.run", lambda *a, **kw: 0)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = m.main(argv)
    assert rc == 0, f"{argv} was rejected"
    return seen


def test_log_follow_composes_with_show(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _follow_driver_kwargs(
        tmp_path, monkeypatch, ["log", _ID, "--follow", "--show", "thinking"])
    assert seen["cats"] == frozenset({"thinking"})


def test_log_follow_composes_with_full_and_hide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _follow_driver_kwargs(
        tmp_path, monkeypatch, ["log", _ID, "-f", "--full", "--hide", "tiers"])
    assert seen["cats"] == _ALL - {"tiers"}


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
