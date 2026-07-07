"""Workflow snapshot refresh on run_change entry (spec: Workflow snapshot
refresh on resume). Covers the content-comparison + crash-safe refresh
helpers and the run_change wiring / workflow_refreshed event."""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.executor import (
    RunResult,
    _refresh_workflow_snapshot,
    _trees_identical,
    run_change,
)
from tests.test_executor import _register_fake, _seed_minimal_workflow


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_trees_identical_matches_same_bytes(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        (d / "sub").mkdir(parents=True)
        (d / "top.txt").write_text("hello")
        (d / "sub" / "n.yaml").write_text("k: 1\n")
    assert _trees_identical(a, b) is True


def test_trees_identical_detects_byte_difference(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "f.txt").write_text("v1")
    (b / "f.txt").write_text("v2")
    assert _trees_identical(a, b) is False


def test_trees_identical_detects_missing_path(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "f.txt").write_text("x")
    (a / "extra.txt").write_text("y")
    (b / "f.txt").write_text("x")
    assert _trees_identical(a, b) is False
