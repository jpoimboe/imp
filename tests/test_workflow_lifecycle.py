"""Pristine content hash + stamp I/O (spec item 3)."""
from __future__ import annotations

import shutil
from pathlib import Path

from imp.assets import bundled_workflow_dir
from imp.engine.workflow_lifecycle import (
    DriftState,
    classify_drift,
    hash_workflow_dir,
    read_pristine,
    workflow_update_notice,
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


def _mirror_of_imp(dest: Path) -> Path:
    shutil.copytree(bundled_workflow_dir("imp"), dest)
    return dest


def test_classify_up_to_date_when_mirror_matches_bundled(tmp_path: Path) -> None:
    mirror = _mirror_of_imp(tmp_path / "imp")
    write_pristine(mirror)
    assert classify_drift("imp", mirror) is DriftState.UP_TO_DATE


def test_classify_update_available_when_clean_mirror_lags_bundled(tmp_path: Path) -> None:
    # A clean mirror (M == P) whose content differs from the real bundled source
    # (B != P) is exactly the update-available case.
    mirror = _mirror_of_imp(tmp_path / "imp")
    (mirror / "workflow.yaml").write_text("name: stale-mirror\n")
    write_pristine(mirror)                      # P := hash(mirror) == M, but != B
    assert classify_drift("imp", mirror) is DriftState.UPDATE_AVAILABLE


def test_classify_locally_modified_when_mirror_differs_from_pristine(tmp_path: Path) -> None:
    mirror = _mirror_of_imp(tmp_path / "imp")
    write_pristine(mirror)
    (mirror / "workflow.yaml").write_text("name: hand-edited\n")   # M != P
    assert classify_drift("imp", mirror) is DriftState.LOCALLY_MODIFIED


def test_classify_locally_modified_when_stamp_absent(tmp_path: Path) -> None:
    mirror = _mirror_of_imp(tmp_path / "mine")     # no .pristine written
    assert classify_drift("imp", mirror) is DriftState.LOCALLY_MODIFIED


def test_classify_fork_with_no_bundled_twin_is_none(tmp_path: Path) -> None:
    mirror = _mirror_of_imp(tmp_path / "mine")
    write_pristine(mirror)
    assert classify_drift("mine", mirror) is None


def test_notice_present_when_update_available(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    mirror = _mirror_of_imp(tmp_path / "imp" / "workflows" / "imp")
    (mirror / "workflow.yaml").write_text("name: stale-mirror\n")
    write_pristine(mirror)                       # clean mirror that lags bundled
    notice = workflow_update_notice(tmp_path)
    assert notice is not None
    assert "update-workflow" in notice


def test_notice_absent_when_up_to_date(tmp_path: Path) -> None:
    (tmp_path / "imp").mkdir()
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: imp\n")
    mirror = _mirror_of_imp(tmp_path / "imp" / "workflows" / "imp")
    write_pristine(mirror)
    assert workflow_update_notice(tmp_path) is None
