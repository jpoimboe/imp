"""Workflow snapshot refresh on run_change entry (spec: Workflow snapshot
refresh on resume). Covers the content-comparison + crash-safe refresh
helpers and the run_change wiring / workflow_refreshed event."""
from __future__ import annotations

import os
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


def test_refresh_fresh_change_makes_initial_copy_no_flag(tmp_path: Path) -> None:
    change, _, _ = _seed_minimal_workflow(tmp_path)
    snapshot = change.change_dir / "workflow"
    assert not snapshot.exists()
    # A fresh change's initial snapshot is NOT a "refresh": returns False so
    # run_change emits no workflow_refreshed event.
    assert _refresh_workflow_snapshot(change) is False
    assert (snapshot / "workflow.yaml").exists()
    assert (snapshot / "agents" / "writer.md").exists()


def test_refresh_identical_source_no_swap(tmp_path: Path) -> None:
    change, _, _ = _seed_minimal_workflow(tmp_path)
    assert _refresh_workflow_snapshot(change) is False        # initial copy
    snapshot = change.change_dir / "workflow"
    before = (snapshot / "workflow.yaml").read_bytes()
    assert _refresh_workflow_snapshot(change) is False        # unchanged source
    assert (snapshot / "workflow.yaml").read_bytes() == before
    assert not (change.change_dir / "workflow.tmp").exists()


def test_refresh_changed_source_replaces_snapshot(tmp_path: Path) -> None:
    change, _, _ = _seed_minimal_workflow(tmp_path)
    _refresh_workflow_snapshot(change)
    live_yaml = tmp_path / "imp" / "workflows" / "imp" / "workflow.yaml"
    live_yaml.write_text(live_yaml.read_text() + "# edited-live\n")
    assert _refresh_workflow_snapshot(change) is True
    snapshot = change.change_dir / "workflow"
    assert "# edited-live" in (snapshot / "workflow.yaml").read_text()
    assert not (change.change_dir / "workflow.tmp").exists()
    assert not (change.change_dir / "workflow.old").exists()


def test_refresh_missing_live_source_keeps_snapshot(tmp_path: Path) -> None:
    change, _, _ = _seed_minimal_workflow(tmp_path)
    _refresh_workflow_snapshot(change)
    snapshot = change.change_dir / "workflow"
    before = (snapshot / "workflow.yaml").read_bytes()
    # Point the default workflow at a name with no committed dir and no bundle:
    # resolve_workflow_dir then RAISES (unknown workflow), so the live source is
    # unavailable and refresh is a best-effort no-op. (Removing the committed
    # imp/workflows/imp does NOT do this — the bundled "imp" workflow is a live
    # fallback, so live_workflow_dir always resolves to an existing dir.)
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: nosuch\n")
    assert _refresh_workflow_snapshot(change) is False
    assert (snapshot / "workflow.yaml").read_bytes() == before


def test_refresh_unresolvable_live_source_keeps_snapshot(tmp_path: Path) -> None:
    change, _, _ = _seed_minimal_workflow(tmp_path)
    _refresh_workflow_snapshot(change)
    snapshot = change.change_dir / "workflow"
    before = (snapshot / "workflow.yaml").read_bytes()
    # Malformed project.yaml makes parse_default_workflow RAISE LoadError,
    # which propagates through live_workflow_dir and is caught by the helper's
    # best-effort except → snapshot kept, returns False. (A MISSING project.yaml
    # would NOT raise: parse_default_workflow returns "imp" for an absent file.)
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: [bad yaml\n")
    assert _refresh_workflow_snapshot(change) is False
    assert (snapshot / "workflow.yaml").read_bytes() == before


def test_refresh_cleans_stale_tmp(tmp_path: Path) -> None:
    change, _, _ = _seed_minimal_workflow(tmp_path)
    _refresh_workflow_snapshot(change)
    tmp = change.change_dir / "workflow.tmp"
    tmp.mkdir()
    (tmp / "stale.yaml").write_text("garbage")
    _refresh_workflow_snapshot(change)
    assert not tmp.exists()
    assert (change.change_dir / "workflow" / "workflow.yaml").exists()


def test_refresh_recovers_workflow_old_when_workflow_missing(tmp_path: Path) -> None:
    change, _, _ = _seed_minimal_workflow(tmp_path)
    _refresh_workflow_snapshot(change)
    snapshot = change.change_dir / "workflow"
    old = change.change_dir / "workflow.old"
    os.rename(snapshot, old)                                   # crash mid-swap
    # Make the live source unresolvable too (unknown workflow name), so the
    # snapshot can ONLY reappear if workflow.old was renamed back — never via a
    # fresh copy. (rmtree of imp/workflows/imp would fall back to the bundled
    # "imp" workflow, which is a live source, defeating the point.)
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: nosuch\n")
    assert not snapshot.exists()
    assert _refresh_workflow_snapshot(change) is False
    assert snapshot.is_dir()
    assert (snapshot / "workflow.yaml").exists()
    assert not old.exists()


def test_refresh_deletes_leftover_workflow_old(tmp_path: Path) -> None:
    change, _, _ = _seed_minimal_workflow(tmp_path)
    _refresh_workflow_snapshot(change)
    snapshot = change.change_dir / "workflow"
    old = change.change_dir / "workflow.old"
    old.mkdir()
    (old / "junk.yaml").write_text("stale")
    _refresh_workflow_snapshot(change)
    assert not old.exists()
    assert (snapshot / "workflow.yaml").exists()
