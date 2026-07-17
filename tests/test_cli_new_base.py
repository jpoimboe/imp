from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import pytest

from imp.cli import main
from imp.cli.main import (
    BaseResolutionError,
    _async_cmd_new,
    _build_parser,
    _record_idea,
    _resolve_new_base,
)
from imp.engine.quest import Quest
from imp.engine.state import State
from tests.conftest import _git, _seed_control


def _repo(root: Path, branch: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", branch)
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")


def _write_default_base(root: Path, value: str) -> None:
    (root / "imp").mkdir(parents=True, exist_ok=True)
    (root / "imp" / "project.yaml").write_text(
        f"default-workflow: imp\ndefault-base: {value}\n")


def test_config_default_base_used_when_no_flag(tmp_path: Path) -> None:
    _repo(tmp_path, "main")
    _git(tmp_path, "branch", "release/9")
    _write_default_base(tmp_path, "release/9")
    assert _resolve_new_base(tmp_path, None) == "release/9"


def test_missing_default_base_raises_pointing_at_init(tmp_path: Path) -> None:
    _repo(tmp_path, "main")  # no default-base written
    with pytest.raises(BaseResolutionError) as exc:
        _resolve_new_base(tmp_path, None)
    assert "imp init" in str(exc.value)


def test_nonexistent_default_base_raises(tmp_path: Path) -> None:
    _repo(tmp_path, "main")
    _write_default_base(tmp_path, "ghost")  # no such branch
    with pytest.raises(BaseResolutionError):
        _resolve_new_base(tmp_path, None)


def test_base_flag_overrides_default_base(tmp_path: Path) -> None:
    _repo(tmp_path, "main")
    _write_default_base(tmp_path, "main")
    _git(tmp_path, "branch", "feature-x")
    assert _resolve_new_base(tmp_path, "feature-x") == "feature-x"


def test_base_flag_nonexistent_raises(tmp_path: Path) -> None:
    _repo(tmp_path, "main")
    with pytest.raises(BaseResolutionError):
        _resolve_new_base(tmp_path, "nope")


def test_base_dot_is_ordinary_name_and_raises(tmp_path: Path) -> None:
    _repo(tmp_path, "main")  # `.` is no longer current-branch magic
    with pytest.raises(BaseResolutionError):
        _resolve_new_base(tmp_path, ".")


def test_base_head_is_ordinary_name_and_raises(tmp_path: Path) -> None:
    _repo(tmp_path, "main")  # `HEAD` is no longer current-branch magic
    with pytest.raises(BaseResolutionError):
        _resolve_new_base(tmp_path, "HEAD")


def test_cmd_new_missing_default_base_exits_2_and_creates_no_draft(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    # Drop the seed's default-base and re-commit so the tree stays clean
    # (check_preconditions runs before base resolution).
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    _git(tmp_path, "commit", "-aqm", "drop default-base")
    monkeypatch.chdir(tmp_path)
    rc = asyncio.run(_async_cmd_new(argparse.Namespace(base=None)))
    assert rc == 2
    assert not (tmp_path / ".imp" / "quests").exists()  # no draft created
    assert "imp init" in capsys.readouterr().err


def test_cmd_new_nonexistent_default_base_exits_2_and_creates_no_draft(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    (tmp_path / "imp" / "project.yaml").write_text(
        "default-workflow: imp\ndefault-base: ghost\n")
    _git(tmp_path, "commit", "-aqm", "point default-base at a missing branch")
    monkeypatch.chdir(tmp_path)
    rc = asyncio.run(_async_cmd_new(argparse.Namespace(base=None)))
    assert rc == 2
    assert not (tmp_path / ".imp" / "quests").exists()  # no draft created
    assert "does not name an existing branch" in capsys.readouterr().err


def test_new_parser_accepts_base_flag() -> None:
    args = _build_parser().parse_args(["new", "do a thing", "--base", "trunk"])
    assert args.base == "trunk"


def test_new_parser_base_defaults_none() -> None:
    args = _build_parser().parse_args(["new", "do a thing"])
    assert args.base is None


def test_new_parser_base_help_drops_dot_magic() -> None:
    sub_action = next(
        a for a in _build_parser()._actions
        if isinstance(a, argparse._SubParsersAction))
    action = next(
        a for a in sub_action.choices["new"]._actions
        if "--base" in a.option_strings)
    assert "--base ." not in (action.help or "")


def test_record_idea_uses_passed_base_not_current_branch(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    _git(tmp_path, "branch", "release/9")
    _git(tmp_path, "checkout", "-b", "work")
    monkeypatch.chdir(tmp_path)
    ns = argparse.Namespace(
        idea="my idea", opening=None, depends_on=None, workflow=None)
    rc = _record_idea(ns, tmp_path, "release/9")
    assert rc == 0
    ids = [p.name for p in (tmp_path / ".imp" / "quests").iterdir()]
    assert len(ids) == 1, ids
    quest = Quest.load(tmp_path, ids[0])
    assert quest.base_branch == "release/9"


def test_apply_new_backlog_records_quest(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc, msg = main.apply_new(
        tmp_path, opening="wire the widget", title=None, workflow=None,
        base=None, idea="Widget work", depends_on=["dep55"])
    assert rc == 0
    assert "recorded backlog quest" in msg
    quests = sorted((tmp_path / ".imp" / "quests").iterdir())
    quest = Quest.load(tmp_path, quests[-1].name)
    meta = json.loads((quest.quest_dir / "quest.json").read_text())
    assert quest.origin == "backlog"
    assert quest.title == "Widget work"
    assert meta["depends_on"] == ["dep55"]


def test_apply_new_draft_records_quest(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc, msg = main.apply_new(
        tmp_path, opening="do the thing", title="Thing", workflow=None,
        base=None, idea=None, depends_on=None)
    assert rc == 0
    assert "created draft" in msg
    quests = sorted((tmp_path / ".imp" / "quests").iterdir())
    quest = Quest.load(tmp_path, quests[-1].name)
    assert quest.origin == "draft"
    assert quest.title == "Thing"
    assert quest.seed_opening == "do the thing"


def test_apply_new_empty_idea_returns_error(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc, msg = main.apply_new(
        tmp_path, opening="x", title=None, workflow=None,
        base=None, idea="   ", depends_on=None)
    assert rc == 2
    assert "non-empty" in msg


def test_apply_new_bad_base_returns_error(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc, msg = main.apply_new(
        tmp_path, opening="x", title=None, workflow=None,
        base="no-such-branch", idea=None, depends_on=None)
    assert rc == 2
    assert "does not name an existing branch" in msg


def test_apply_new_backlog_defers_seed_and_records_parent(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc, _msg = main.apply_new(
        tmp_path, opening="brief", title=None, workflow=None, base=None,
        idea="Child work", depends_on=None, parent="mom7")
    assert rc == 0
    quests = sorted((tmp_path / ".imp" / "quests").iterdir())
    quest = Quest.load(tmp_path, quests[-1].name)
    meta = json.loads((quest.quest_dir / "quest.json").read_text())
    assert meta["parent"] == "mom7"
    assert State.load(quest.quest_dir / "state").step is None


def test_apply_new_draft_defers_seed(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc, _ = main.apply_new(
        tmp_path, opening="do it", title="Thing", workflow=None, base=None,
        idea=None, depends_on=None, parent="pdraft")
    assert rc == 0
    quests = sorted((tmp_path / ".imp" / "quests").iterdir())
    quest = Quest.load(tmp_path, quests[-1].name)
    assert State.load(quest.quest_dir / "state").step is None
    meta = json.loads((quest.quest_dir / "quest.json").read_text())
    assert meta["parent"] == "pdraft"


def test_apply_new_background_starts_quest(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    started: dict[str, object] = {}
    import imp.remote.daemon.spawn as spawn_mod
    monkeypatch.setattr(
        spawn_mod, "start_quest",
        lambda control, qid: started.update(control=control, qid=qid))
    rc, msg = main.apply_new(
        tmp_path, opening="do the thing", title="Task", workflow=None,
        base=None, idea=None, depends_on=None, background=True, parent="mom3")
    assert rc == 0
    assert "started quest" in msg
    quests = sorted((tmp_path / ".imp" / "quests").iterdir())
    quest = Quest.load(tmp_path, quests[-1].name)
    assert quest.origin == "draft"
    assert started["qid"] == quest.id
    meta = json.loads((quest.quest_dir / "quest.json").read_text())
    assert meta["parent"] == "mom3"
    assert started["control"] == tmp_path
    assert State.load(quest.quest_dir / "state").step is None
