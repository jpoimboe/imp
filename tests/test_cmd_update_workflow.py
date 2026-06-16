"""imp update-workflow: copies bundled source, re-stamps .pristine, commits."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from imp.assets import bundled_workflow_dir
from imp.engine.workflow_lifecycle import (
    DriftState,
    classify_drift,
    write_pristine,
)
from tests.conftest import _git, _seed_control


def _stale_mirror(root: Path) -> Path:
    """Project with a clean (stamped) but lagging mirror."""
    _seed_control(root)
    mirror = root / "imp" / "workflows" / "imp"
    (mirror / "workflow.yaml").write_text("name: stale-mirror\n")
    write_pristine(mirror)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "stale mirror")
    return mirror


def _modified_mirror(root: Path) -> Path:
    """Project with a locally-modified mirror (no stamp match)."""
    _seed_control(root)
    mirror = root / "imp" / "workflows" / "imp"
    write_pristine(mirror)
    (mirror / "workflow.yaml").write_text("name: hand-edited\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "modified mirror")
    return mirror


def test_update_workflow_succeeds_and_commits(tmp_path: Path, monkeypatch) -> None:
    mirror = _stale_mirror(tmp_path)
    monkeypatch.chdir(tmp_path)
    from imp.cli.main import _cmd_update_workflow
    rc = _cmd_update_workflow(argparse.Namespace())
    assert rc == 0
    assert classify_drift("imp", mirror) is DriftState.UP_TO_DATE
    # The commit happened.
    import subprocess
    log = subprocess.run(["git", "log", "--oneline", "-1"], cwd=tmp_path,
                        capture_output=True, text=True).stdout.strip()
    assert "chore: update imp workflow" in log.lower()
    # Worktree must be clean — .pristine was included in the commit.
    status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path,
                     capture_output=True, text=True).stdout.strip()
    assert status == "", f"worktree not clean after update: {status!r}"
    ls = subprocess.run(["git", "ls-files", str(mirror / ".pristine")],
                 cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    assert ls != "", ".pristine not tracked in HEAD after update"


def test_update_workflow_no_op_when_up_to_date(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    mirror = tmp_path / "imp" / "workflows" / "imp"
    shutil.rmtree(mirror)
    shutil.copytree(bundled_workflow_dir("imp"), mirror)
    write_pristine(mirror)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "up to date")
    monkeypatch.chdir(tmp_path)
    from imp.cli.main import _cmd_update_workflow
    rc = _cmd_update_workflow(argparse.Namespace())
    assert rc == 0
    assert "already up-to-date" in capsys.readouterr().out.lower()


def test_update_workflow_refuses_locally_modified(tmp_path: Path, monkeypatch, capsys) -> None:
    _modified_mirror(tmp_path)
    monkeypatch.chdir(tmp_path)
    from imp.cli.main import _cmd_update_workflow
    rc = _cmd_update_workflow(argparse.Namespace())
    assert rc == 1
    assert "locally modified" in capsys.readouterr().err.lower()


def test_update_workflow_refuses_unmanaged_fork(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_control(tmp_path)
    # Rename to a non-bundled name.
    _git(tmp_path, "mv", "imp/workflows/imp", "imp/workflows/mine")
    (tmp_path / "imp" / "project.yaml").write_text("default-workflow: mine\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "fork")
    monkeypatch.chdir(tmp_path)
    from imp.cli.main import _cmd_update_workflow
    rc = _cmd_update_workflow(argparse.Namespace())
    assert rc == 1
    assert "not a managed" in capsys.readouterr().err.lower()


def test_update_workflow_commit_scoped_to_mirror(tmp_path: Path, monkeypatch) -> None:
    _stale_mirror(tmp_path)
    import subprocess as _sp
    # Stage an unrelated file that must NOT be swept into the workflow commit.
    extra = tmp_path / "unrelated.txt"
    extra.write_text("unrelated\n")
    _sp.run(["git", "add", str(extra)], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    from imp.cli.main import _cmd_update_workflow
    rc = _cmd_update_workflow(argparse.Namespace())
    assert rc == 0
    # The workflow commit must not contain the unrelated file.
    committed = _sp.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True,
    ).stdout.strip().splitlines()
    assert not any("unrelated" in f for f in committed)
    # The unrelated file remains staged.
    status = _sp.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True,
    ).stdout
    assert "unrelated.txt" in status


def test_update_workflow_commit_failure_is_recoverable(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    mirror = _stale_mirror(tmp_path)
    # A pre-commit hook that always rejects commits.
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    from imp.cli.main import _cmd_update_workflow
    rc = _cmd_update_workflow(argparse.Namespace())
    assert rc == 2
    # Mirror must be restored to HEAD so classify_drift sees UPDATE_AVAILABLE,
    # not LOCALLY_MODIFIED — retries must work.
    assert classify_drift("imp", mirror) is DriftState.UPDATE_AVAILABLE
