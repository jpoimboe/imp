"""Pristine content hash + stamp I/O (spec item 3)."""
from __future__ import annotations

from pathlib import Path

from imp.engine.workflow_lifecycle import (
    hash_workflow_dir,
    read_pristine,
    write_pristine,
)


def _mk(d: Path) -> None:
    (d / "agents").mkdir(parents=True)
    (d / "workflow.yaml").write_text("name: t\n")
    (d / "agents" / "a.md").write_text("alpha\n")


def test_hash_changes_when_a_file_is_added(tmp_path: Path) -> None:
    d = tmp_path / "wf"
    _mk(d)
    h1 = hash_workflow_dir(d)
    (d / "agents" / "b.md").write_text("beta\n")
    assert hash_workflow_dir(d) != h1


def test_hash_changes_when_a_file_is_edited(tmp_path: Path) -> None:
    d = tmp_path / "wf"
    _mk(d)
    h1 = hash_workflow_dir(d)
    (d / "agents" / "a.md").write_text("ALPHA-EDITED\n")
    assert hash_workflow_dir(d) != h1


def test_hash_changes_when_a_file_is_removed(tmp_path: Path) -> None:
    d = tmp_path / "wf"
    _mk(d)
    h1 = hash_workflow_dir(d)
    (d / "agents" / "a.md").unlink()
    assert hash_workflow_dir(d) != h1


def test_hash_ignores_the_pristine_stamp(tmp_path: Path) -> None:
    d = tmp_path / "wf"
    _mk(d)
    h1 = hash_workflow_dir(d)
    (d / ".pristine").write_text("anything at all\n")
    assert hash_workflow_dir(d) == h1


def test_write_then_read_pristine_round_trips_to_the_hash(tmp_path: Path) -> None:
    d = tmp_path / "wf"
    _mk(d)
    write_pristine(d)
    assert read_pristine(d) == hash_workflow_dir(d)


def test_read_pristine_absent_is_none(tmp_path: Path) -> None:
    d = tmp_path / "wf"
    _mk(d)
    assert read_pristine(d) is None
