from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine.loader import ALLOWABLE_TOOLS
from imp.engine.quest import Quest
from tests.conftest import _seed_control


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout


def _quest(tmp_path: Path) -> Quest:
    _seed_control(tmp_path)
    return Quest.create(
        tmp_path, title="t", quest_id="aaa111", base_branch="main"
    )


def _ctx(quest: Quest | None, control_root: Path) -> ToolContext:
    ctx = ToolContext(
        project_root=control_root, events_path=control_root / "events.jsonl"
    )
    ctx.quest = quest
    return ctx


def _invoke(ctx: ToolContext, **args: str) -> str:
    return get_registry()["commit_memory"].invoke(ctx, args)


def test_tool_is_registered_and_grantable() -> None:
    assert "commit_memory" in get_registry()
    assert "commit_memory" in ALLOWABLE_TOOLS


def test_commits_single_valid_file(tmp_path: Path) -> None:
    quest = _quest(tmp_path)
    result = _invoke(
        _ctx(quest, tmp_path),
        path="imp/s.md",
        content="prefer prose\n",
        message="memory: prefer prose at step s",
    )
    written = tmp_path / "imp" / "memory" / "imp" / "s.md"
    assert written.read_text() == "prefer prose\n"
    # HEAD commit is exactly the message, on branch main, touching only the file.
    assert _git(tmp_path, "log", "-1", "--pretty=%s").strip() == (
        "memory: prefer prose at step s"
    )
    assert _git(tmp_path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    names = _git(tmp_path, "show", "--name-only", "--pretty=format:", "HEAD")
    assert names.split() == ["imp/memory/imp/s.md"]
    assert "imp/memory/imp/s.md" in result
    assert "main" in result


def test_pathspec_excludes_unrelated_staged_state(tmp_path: Path) -> None:
    quest = _quest(tmp_path)
    (tmp_path / "unrelated.txt").write_text("wip\n")
    _git(tmp_path, "add", "unrelated.txt")

    _invoke(
        _ctx(quest, tmp_path),
        path="all.md",
        content="global pref\n",
        message="memory: global pref",
    )

    # The new commit touches only the memory file.
    names = _git(tmp_path, "show", "--name-only", "--pretty=format:", "HEAD")
    assert names.split() == ["imp/memory/all.md"]
    # The unrelated file is still staged and uncommitted.
    status = _git(tmp_path, "status", "--short")
    assert "unrelated.txt" in status


def test_unknown_workflow_writes_nothing(tmp_path: Path) -> None:
    quest = _quest(tmp_path)
    before = _git(tmp_path, "rev-parse", "HEAD").strip()
    result = _invoke(
        _ctx(quest, tmp_path),
        path="nope/all.md",
        content="x\n",
        message="memory: bad",
    )
    assert "nope" in result  # error text names the offending path
    assert not (tmp_path / "imp" / "memory" / "nope").exists()
    assert _git(tmp_path, "rev-parse", "HEAD").strip() == before


def test_non_step_file_writes_nothing(tmp_path: Path) -> None:
    quest = _quest(tmp_path)
    before = _git(tmp_path, "rev-parse", "HEAD").strip()
    result = _invoke(
        _ctx(quest, tmp_path),
        path="imp/notastep.md",
        content="x\n",
        message="memory: bad",
    )
    assert "notastep" in result
    assert not (tmp_path / "imp" / "memory" / "imp" / "notastep.md").exists()
    assert _git(tmp_path, "rev-parse", "HEAD").strip() == before


def test_refuses_traversal_path(tmp_path: Path) -> None:
    quest = _quest(tmp_path)
    before = _git(tmp_path, "rev-parse", "HEAD").strip()
    result = _invoke(
        _ctx(quest, tmp_path),
        path="../secrets.md",
        content="x\n",
        message="memory: escape",
    )
    assert "refuses" in result
    assert not (tmp_path / "secrets.md").exists()
    assert _git(tmp_path, "rev-parse", "HEAD").strip() == before


def test_refuses_absolute_path(tmp_path: Path) -> None:
    quest = _quest(tmp_path)
    before = _git(tmp_path, "rev-parse", "HEAD").strip()
    result = _invoke(
        _ctx(quest, tmp_path),
        path="/etc/evil.md",
        content="x\n",
        message="memory: escape",
    )
    assert "refuses" in result
    assert _git(tmp_path, "rev-parse", "HEAD").strip() == before


def test_refuses_dot_imp_path(tmp_path: Path) -> None:
    quest = _quest(tmp_path)
    before = _git(tmp_path, "rev-parse", "HEAD").strip()
    result = _invoke(
        _ctx(quest, tmp_path),
        path=".imp/foo.md",
        content="x\n",
        message="memory: escape",
    )
    assert "refuses" in result
    assert not (tmp_path / ".imp" / "foo.md").exists()
    assert _git(tmp_path, "rev-parse", "HEAD").strip() == before


def test_refuses_empty_path(tmp_path: Path) -> None:
    quest = _quest(tmp_path)
    before = _git(tmp_path, "rev-parse", "HEAD").strip()
    result = _invoke(
        _ctx(quest, tmp_path),
        path="",
        content="x\n",
        message="memory: escape",
    )
    assert "refuses" in result
    assert _git(tmp_path, "rev-parse", "HEAD").strip() == before


def test_refuses_dot_path(tmp_path: Path) -> None:
    quest = _quest(tmp_path)
    before = _git(tmp_path, "rev-parse", "HEAD").strip()
    result = _invoke(
        _ctx(quest, tmp_path),
        path=".",
        content="x\n",
        message="memory: escape",
    )
    assert "refuses" in result
    assert _git(tmp_path, "rev-parse", "HEAD").strip() == before


def test_identical_content_is_noop(tmp_path: Path) -> None:
    quest = _quest(tmp_path)
    _invoke(
        _ctx(quest, tmp_path),
        path="imp/s.md",
        content="stable memory\n",
        message="memory: initial",
    )
    sha_after_first = _git(tmp_path, "rev-parse", "HEAD").strip()

    result = _invoke(
        _ctx(quest, tmp_path),
        path="imp/s.md",
        content="stable memory\n",
        message="memory: initial",
    )

    assert "nothing to commit" in result
    assert _git(tmp_path, "rev-parse", "HEAD").strip() == sha_after_first


def test_top_level_only_guard(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    with pytest.raises(RuntimeError):
        _invoke(
            _ctx(None, tmp_path),
            path="all.md",
            content="x\n",
            message="memory: x",
        )
