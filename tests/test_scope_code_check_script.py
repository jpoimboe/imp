"""The scope-code-check gate: after scoping hands off, any commit on the quest
branch beyond the fork point (cur_base_head) is illegitimate. Clean -> exit 0;
committed code -> exit 1 with a recovery message on stderr."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from imp.assets import bundled_workflow_dir

SCRIPT = bundled_workflow_dir("default") / "scripts" / "scope-code-check.sh"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True,
                   capture_output=True, text=True)


def _init_repo(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "f.txt").write_text("base\n")
    _git(root, "add", "f.txt")
    _git(root, "commit", "-q", "-m", "base")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root,
        capture_output=True, text=True,
    ).stdout.strip()


def _quest_dir(tmp_path: Path, fork: str) -> Path:
    qd = tmp_path / "home"
    qd.mkdir(parents=True, exist_ok=True)
    (qd / "quest.json").write_text(json.dumps({"cur_base_head": fork}))
    return qd


def _run(root: Path, quest_dir: Path,
         path: str = os.environ["PATH"]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=root,
        env={"IMP_PROJECT_ROOT": str(root), "IMP_QUEST_DIR": str(quest_dir),
             "IMP_QUEST_ID": "id1", "IMP_PYTHON": sys.executable, "PATH": path},
        capture_output=True, text=True,
    )


def test_clean_handoff_passes(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    fork = _init_repo(root)
    qd = _quest_dir(tmp_path, fork)
    proc = _run(root, qd)
    assert proc.returncode == 0, proc.stderr


def test_committed_code_fails_with_recovery_message(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    fork = _init_repo(root)
    # Scoping errantly commits code beyond the fork point.
    (root / "leak.py").write_text("x = 1\n")
    _git(root, "add", "leak.py")
    _git(root, "commit", "-q", "-m", "leak: committed during scoping")
    qd = _quest_dir(tmp_path, fork)

    proc = _run(root, qd)
    assert proc.returncode == 1
    # Explanatory text + offending commit + fork-named recovery command, all on stderr.
    assert "committed code to the quest branch" in proc.stderr
    assert "leak: committed during scoping" in proc.stderr
    assert f"git reset --hard {fork}" in proc.stderr


def test_missing_fork_fails(tmp_path: Path) -> None:
    # An empty cur_base_head is a hard error, not a silent pass.
    root = tmp_path / "proj"
    _init_repo(root)
    qd = _quest_dir(tmp_path, "")
    proc = _run(root, qd)
    assert proc.returncode == 1


def test_invalid_fork_fails(tmp_path: Path) -> None:
    # A well-formed but nonexistent fork SHA makes `git rev-list` error; the gate
    # must fail closed (exit 1), never read a git error as a clean tree.
    root = tmp_path / "proj"
    _init_repo(root)
    qd = _quest_dir(tmp_path, "deadbeef" * 5)
    proc = _run(root, qd)
    assert proc.returncode == 1


def test_reads_the_fork_without_a_working_python3_on_path(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    fork = _init_repo(root)
    (root / "leak.py").write_text("x = 1\n")
    _git(root, "add", "leak.py")
    _git(root, "commit", "-q", "-m", "leak")
    qd = _quest_dir(tmp_path, fork)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name in ("python3", "python"):
        stub = stubs / name
        stub.write_text("#!/bin/sh\n"
                        "echo \"ModuleNotFoundError: No module named 'imp.providers'\" >&2\n"
                        "exit 1\n")
        stub.chmod(0o755)

    proc = _run(root, qd, path=f"{stubs}{os.pathsep}{os.environ['PATH']}")
    assert proc.returncode == 1
    assert f"git reset --hard {fork}" in proc.stderr
