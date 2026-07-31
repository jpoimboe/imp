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
from imp.paths import project_state_dir
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
    (root / "imp" / "config.yaml").write_text(
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
    (tmp_path / "imp" / "config.yaml").write_text("default-workflow: imp\n")
    _git(tmp_path, "commit", "-aqm", "drop default-base")
    monkeypatch.chdir(tmp_path)
    rc = asyncio.run(_async_cmd_new(argparse.Namespace(base=None)))
    assert rc == 2
    assert not (project_state_dir(tmp_path) / "quests").exists()  # no draft created
    assert "imp init" in capsys.readouterr().err


def test_cmd_new_nonexistent_default_base_exits_2_and_creates_no_draft(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    (tmp_path / "imp" / "config.yaml").write_text(
        "default-workflow: imp\ndefault-base: ghost\n")
    _git(tmp_path, "commit", "-aqm", "point default-base at a missing branch")
    monkeypatch.chdir(tmp_path)
    rc = asyncio.run(_async_cmd_new(argparse.Namespace(base=None)))
    assert rc == 2
    assert not (project_state_dir(tmp_path) / "quests").exists()  # no draft created
    assert "does not name an existing branch" in capsys.readouterr().err


def test_cmd_new_passes_resolved_base_to_ensure_imp_ignored(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    _git(tmp_path, "branch", "release/9")
    (tmp_path / "imp" / "config.yaml").write_text(
        "default-workflow: imp\ndefault-base: release/9\n")
    _git(tmp_path, "commit", "-aqm", "point default-base at release/9")
    monkeypatch.chdir(tmp_path)
    seen: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        "imp.engine.worktree.ensure_imp_ignored",
        lambda control, base: seen.append((control, base)))

    # An invalid workflow name aborts right AFTER the ignore repair, so the
    # call is observable without starting a session.
    rc = asyncio.run(_async_cmd_new(argparse.Namespace(base=None, workflow="a/b")))

    assert rc == 2
    assert [base for _, base in seen] == ["release/9"]


def test_cmd_new_skips_ignore_repair_when_base_unresolvable(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    (tmp_path / "imp" / "config.yaml").write_text(
        "default-workflow: imp\ndefault-base: ghost\n")
    _git(tmp_path, "commit", "-aqm", "point default-base at a missing branch")
    monkeypatch.chdir(tmp_path)
    seen: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        "imp.engine.worktree.ensure_imp_ignored", lambda *a: seen.append(a))

    rc = asyncio.run(_async_cmd_new(argparse.Namespace(base=None)))

    assert rc == 2
    assert seen == []


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
    ids = [p.name for p in (project_state_dir(tmp_path) / "quests").iterdir()]
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
    quests = sorted((project_state_dir(tmp_path) / "quests").iterdir())
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
    assert "recorded backlog quest" in msg
    quests = sorted((project_state_dir(tmp_path) / "quests").iterdir())
    quest = Quest.load(tmp_path, quests[-1].name)
    assert quest.origin == "backlog"
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
    quests = sorted((project_state_dir(tmp_path) / "quests").iterdir())
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
    quests = sorted((project_state_dir(tmp_path) / "quests").iterdir())
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
    quests = sorted((project_state_dir(tmp_path) / "quests").iterdir())
    quest = Quest.load(tmp_path, quests[-1].name)
    assert quest.origin == "draft"
    assert started["qid"] == quest.id
    meta = json.loads((quest.quest_dir / "quest.json").read_text())
    assert meta["parent"] == "mom3"
    assert started["control"] == tmp_path
    assert State.load(quest.quest_dir / "state").step is None


def _new_subparser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    sub = next(a for a in parser._actions
               if getattr(a, "choices", None) and "new" in a.choices)
    return sub.choices["new"]


def test_new_parser_accepts_background() -> None:
    args = main._build_parser().parse_args(["new", "--background", "brief"])
    assert args.background is True


def test_new_background_help_verbatim() -> None:
    p_new = _new_subparser(main._build_parser())
    bg = next(a for a in p_new._actions if a.dest == "background")
    assert bg.help == (
        "Create the quest and launch it detached in the background, "
        "running headless and autonomous; it appears in the active list. "
        "Implies --headless and --autonomous."
    )


def test_new_depends_on_help_mentions_go_live() -> None:
    p_new = _new_subparser(main._build_parser())
    dep = next(a for a in p_new._actions if a.dest == "depends_on")
    assert dep.help == (
        "A quest id this quest depends on (repeatable). "
        "Gates go-live until the dep merges; never changes the base branch."
    )


def test_new_rejects_backlog_with_background(capsys) -> None:
    with pytest.raises(SystemExit):
        main._build_parser().parse_args(
            ["new", "--backlog", "x", "--background"])
    assert "not allowed with" in capsys.readouterr().err


def test_record_idea_defers_seed(tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    ns = argparse.Namespace(
        idea="my idea", opening=None, depends_on=None, workflow=None)
    rc = _record_idea(ns, tmp_path, "main")
    assert rc == 0
    ids = [p.name for p in (project_state_dir(tmp_path) / "quests").iterdir()]
    quest = Quest.load(tmp_path, ids[0])
    assert State.load(quest.quest_dir / "state").step is None


def _seed_detachable_control(tmp_path: Path) -> None:
    """A seeded control root whose default-base is `release/9`, tree clean."""
    _seed_control(tmp_path)
    _git(tmp_path, "branch", "release/9")
    (tmp_path / "imp" / "config.yaml").write_text(
        "default-workflow: imp\ndefault-base: release/9\n")
    _git(tmp_path, "commit", "-aqm", "point default-base at release/9")


@pytest.mark.parametrize("leave_mode", ["new", "chat"])
def test_cmd_new_detached_control_root_announces_the_base(
        tmp_path: Path, monkeypatch, capsys, leave_mode) -> None:
    _seed_detachable_control(tmp_path)
    _git(tmp_path, "checkout", "-q", "--detach")
    monkeypatch.chdir(tmp_path)

    # An invalid workflow name aborts right AFTER the notice, so it is
    # observable without starting a session.
    rc = asyncio.run(_async_cmd_new(
        argparse.Namespace(base=None, workflow="a/b"), leave_mode=leave_mode))

    assert rc == 2
    assert capsys.readouterr().out.splitlines() == [
        "imp: main checkout is at a detached HEAD — this quest still forks "
        "from release/9."]


def test_cmd_new_on_a_branch_prints_no_detached_notice(
        tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_detachable_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    rc = asyncio.run(_async_cmd_new(argparse.Namespace(base=None, workflow="a/b")))

    assert rc == 2
    assert capsys.readouterr().out == ""
