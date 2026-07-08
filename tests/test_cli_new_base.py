from __future__ import annotations

import argparse
from pathlib import Path

from imp.cli.main import _build_parser, _record_idea, _resolve_new_base
from imp.engine.change import Change
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


def test_explicit_base_flag_wins_over_everything(tmp_path: Path) -> None:
    _repo(tmp_path, "main")
    _write_default_base(tmp_path, "release/9")
    base, notice = _resolve_new_base(tmp_path, "feature-x")
    assert base == "feature-x"
    assert notice is None


def test_base_dot_means_current_branch(tmp_path: Path) -> None:
    _repo(tmp_path, "main")
    assert _resolve_new_base(tmp_path, ".") == ("main", None)


def test_base_head_means_current_branch(tmp_path: Path) -> None:
    _repo(tmp_path, "main")
    assert _resolve_new_base(tmp_path, "HEAD") == ("main", None)


def test_config_default_base_used_when_no_flag(tmp_path: Path) -> None:
    _repo(tmp_path, "main")
    _write_default_base(tmp_path, "release/9")
    base, notice = _resolve_new_base(tmp_path, None)
    assert base == "release/9"
    assert notice == (
        "ℹ basing on release/9  (you're on main — use --base . to base here)")


def test_config_default_base_equal_current_no_notice(tmp_path: Path) -> None:
    _repo(tmp_path, "main")
    _write_default_base(tmp_path, "main")
    assert _resolve_new_base(tmp_path, None) == ("main", None)


def test_auto_detect_origin_head_when_no_config(tmp_path: Path) -> None:
    _repo(tmp_path, "main")
    # `symbolic-ref <name> <target>` writes the ref without requiring <target>
    # to exist; a non-`main` target proves origin/HEAD is what resolved.
    _git(tmp_path, "symbolic-ref",
         "refs/remotes/origin/HEAD", "refs/remotes/origin/upstream")
    base, notice = _resolve_new_base(tmp_path, None)
    assert base == "upstream"
    assert notice == (
        "ℹ basing on upstream  (you're on main — use --base . to base here)")


def test_auto_detect_prefers_master_over_main(tmp_path: Path) -> None:
    _repo(tmp_path, "work")
    _git(tmp_path, "branch", "master")
    _git(tmp_path, "branch", "main")
    base, notice = _resolve_new_base(tmp_path, None)
    assert base == "master"
    assert notice == (
        "ℹ basing on master  (you're on work — use --base . to base here)")


def test_auto_detect_main_when_master_absent(tmp_path: Path) -> None:
    _repo(tmp_path, "work")
    _git(tmp_path, "branch", "main")
    base, notice = _resolve_new_base(tmp_path, None)
    assert base == "main"
    assert notice == (
        "ℹ basing on main  (you're on work — use --base . to base here)")


def test_fallback_to_current_head_when_nothing_resolves(tmp_path: Path) -> None:
    _repo(tmp_path, "solo")
    assert _resolve_new_base(tmp_path, None) == ("solo", None)


def test_new_parser_accepts_base_flag() -> None:
    args = _build_parser().parse_args(["new", "do a thing", "--base", "trunk"])
    assert args.base == "trunk"


def test_new_parser_base_defaults_none() -> None:
    args = _build_parser().parse_args(["new", "do a thing"])
    assert args.base is None


def test_record_idea_uses_passed_base_not_current_branch(
        tmp_path: Path, monkeypatch) -> None:
    _seed_control(tmp_path)
    _git(tmp_path, "branch", "release/9")
    _git(tmp_path, "checkout", "-b", "work")
    monkeypatch.chdir(tmp_path)
    ns = argparse.Namespace(idea="my idea", opening=None, depends_on=None, workflow=None)
    rc = _record_idea(ns, tmp_path, "release/9")
    assert rc == 0
    ids = [p.name for p in (tmp_path / ".imp" / "changes").iterdir()]
    assert len(ids) == 1, ids
    change_id = ids[0]
    change = Change.load(tmp_path, change_id)
    assert change.base_branch == "release/9"
