"""The project state-directory seam: slug claiming and the IMP_STATE_DIR override.

IMP_HOME is isolated per test by conftest's `_isolate_global_config`, so claiming
resolves under each test's own tmp_path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.paths import imp_home, project_state_dir


def _repo(parent: Path, name: str) -> Path:
    root = parent / name
    root.mkdir(parents=True)
    return root


def test_claim_creates_the_slug_dir_and_writes_the_repo_marker(tmp_path: Path) -> None:
    root = _repo(tmp_path, "widget")
    state = project_state_dir(root)
    assert state == imp_home() / "projects" / "widget"
    assert state.is_dir()
    assert (state / "repo").read_text() == f"{root.resolve()}\n"


def test_reresolving_the_same_root_returns_the_same_directory(tmp_path: Path) -> None:
    root = _repo(tmp_path, "widget")
    first = project_state_dir(root)
    second = project_state_dir(root)
    assert second == first
    assert [p.name for p in (imp_home() / "projects").iterdir()] == ["widget"]


@pytest.mark.parametrize(
    ("basename", "slug"),
    [
        ("Widget Co", "widget-co"),
        ("My.Repo_1", "my.repo_1"),
        ("Ünïcode~repo", "-n-code-repo"),
        ("a" * 70, "a" * 64),
    ],
)
def test_slug_sanitizes_the_basename(tmp_path: Path, basename: str, slug: str) -> None:
    root = _repo(tmp_path, basename)
    assert project_state_dir(root) == imp_home() / "projects" / slug


def test_a_root_with_no_basename_slugs_to_project() -> None:
    assert project_state_dir(Path("/")) == imp_home() / "projects" / "project"
