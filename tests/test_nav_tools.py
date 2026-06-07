# tests/test_nav_tools.py
from __future__ import annotations

from pathlib import Path

import imp.agents.tools.nav  # noqa: F401
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        project_root=tmp_path,
        fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
    )


def test_glob_finds_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "c.txt").write_text("")
    out = get_registry()["glob"].invoke(_ctx(tmp_path), {"pattern": "/project/*.py"})
    assert "/project/a.py" in out
    assert "/project/b.py" in out
    assert "/project/c.txt" not in out


def test_glob_recursive(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.md").write_text("")
    out = get_registry()["glob"].invoke(_ctx(tmp_path), {"pattern": "/project/**/*.md"})
    assert "/project/sub/x.md" in out


def test_grep_finds_match(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("hello world\nfoo bar\n")
    (tmp_path / "b.py").write_text("nothing\n")
    out = get_registry()["grep"].invoke(
        _ctx(tmp_path), {"pattern": "hello", "path": "/project"}
    )
    assert "a.py" in out
    assert "b.py" not in out


def test_ls_lists_dir(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "sub").mkdir()
    out = get_registry()["ls"].invoke(_ctx(tmp_path), {"path": "/project"})
    assert "a.py" in out
    assert "sub" in out
