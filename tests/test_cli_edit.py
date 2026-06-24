from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from imp.cli import main
from imp.engine import worktree
from imp.engine.change import Change
from tests.conftest import _seed_control


def _make_draft(control: Path, change_id: str, *, origin: str = "draft") -> Change:
    base = worktree.current_branch(control)
    return Change.create_draft(
        control, title="Original", change_id=change_id, base_branch=base,
        origin=origin)


def _meta(change: Change) -> dict:
    return json.loads((change.change_dir / "change.json").read_text())


def _edit_args(token: str, **kw) -> argparse.Namespace:
    base = dict(token=token, title=None, backlog=False, active=False,
                depends_on=None, clear_deps=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_edit_title_rewrites_title(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit01")
    rc = main._cmd_edit(_edit_args("2026-06-24-edit01", title="Renamed"))
    assert rc == 0
    assert Change.load(tmp_path, "2026-06-24-edit01").title == "Renamed"


def test_edit_backlog_moves_to_idea_zone(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit02", origin="draft")
    rc = main._cmd_edit(_edit_args("2026-06-24-edit02", backlog=True))
    assert rc == 0
    assert Change.load(tmp_path, "2026-06-24-edit02").origin == "idea"


def test_edit_active_moves_to_draft_zone(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit03", origin="idea")
    rc = main._cmd_edit(_edit_args("2026-06-24-edit03", active=True))
    assert rc == 0
    assert Change.load(tmp_path, "2026-06-24-edit03").origin == "draft"


def test_edit_zone_rejected_for_materialized(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    change = _make_draft(tmp_path, "2026-06-24-edit04", origin="draft")
    meta = _meta(change)
    meta["materialized"] = True
    (change.change_dir / "change.json").write_text(json.dumps(meta, indent=2,
                                                              sort_keys=True))
    rc = main._cmd_edit(_edit_args("2026-06-24-edit04", backlog=True))
    assert rc == 2
    assert "imp:" in capsys.readouterr().err
    assert Change.load(tmp_path, "2026-06-24-edit04").origin == "draft"


def test_edit_depends_on_replaces_list(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit05")
    rc = main._cmd_edit(_edit_args("2026-06-24-edit05", depends_on=["A", "B"]))
    assert rc == 0
    assert _meta(Change.load(tmp_path, "2026-06-24-edit05"))["depends_on"] == ["A", "B"]


def test_edit_clear_deps_empties_list(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    change = _make_draft(tmp_path, "2026-06-24-edit06")
    change.set_depends_on(["A"])
    rc = main._cmd_edit(_edit_args("2026-06-24-edit06", clear_deps=True))
    assert rc == 0
    assert _meta(Change.load(tmp_path, "2026-06-24-edit06"))["depends_on"] == []


def test_edit_no_flags_is_error(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit07")
    rc = main._cmd_edit(_edit_args("2026-06-24-edit07"))
    assert rc == 2
    assert "imp:" in capsys.readouterr().err
    assert Change.load(tmp_path, "2026-06-24-edit07").title == "Original"


def test_edit_combined_title_zone_and_deps(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    _make_draft(tmp_path, "2026-06-24-edit08", origin="draft")
    rc = main._cmd_edit(_edit_args(
        "2026-06-24-edit08", title="Combined", backlog=True, depends_on=["X"]))
    assert rc == 0
    change = Change.load(tmp_path, "2026-06-24-edit08")
    assert change.title == "Combined"
    assert change.origin == "idea"
    assert _meta(change)["depends_on"] == ["X"]


def test_edit_materialized_title_and_zone_rejects_whole_invocation(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    change = _make_draft(tmp_path, "2026-06-24-edit09", origin="draft")
    meta = _meta(change)
    meta["materialized"] = True
    (change.change_dir / "change.json").write_text(json.dumps(meta, indent=2,
                                                              sort_keys=True))
    rc = main._cmd_edit(
        _edit_args("2026-06-24-edit09", title="Renamed", backlog=True))
    assert rc == 2
    assert "imp:" in capsys.readouterr().err
    reloaded = Change.load(tmp_path, "2026-06-24-edit09")
    assert reloaded.title == "Original"
    assert reloaded.origin == "draft"


def test_edit_backlog_and_active_are_mutually_exclusive() -> None:
    parser = main._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["edit", "2026-06-24-x", "--backlog", "--active"])


def test_edit_depends_on_and_clear_deps_are_mutually_exclusive() -> None:
    parser = main._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["edit", "2026-06-24-x", "--depends-on", "A", "--clear-deps"])
