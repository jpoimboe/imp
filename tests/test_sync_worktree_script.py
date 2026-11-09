from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path("src/imp/assets/workflows/default/scripts/sync-worktree.sh").resolve()


def _run(tmp_path: Path, setup: str | None,
         extra_env: dict[str, str] | None = None,
         base_branch: str | None = "trunk") -> subprocess.CompletedProcess[str]:
    project_root = tmp_path / "wt"
    project_root.mkdir(exist_ok=True)
    quest_dir = tmp_path / "quest"
    quest_dir.mkdir(exist_ok=True)
    if base_branch is not None:
        (quest_dir / "quest.json").write_text(json.dumps({"id": "x", "base_branch": base_branch}))
    env = {k: v for k, v in os.environ.items() if k != "IMP_WORKTREE_SETUP"}
    env.update({"IMP_PROJECT_ROOT": str(project_root), "IMP_QUEST_DIR": str(quest_dir),
                "IMP_PYTHON": sys.executable})
    if setup is not None:
        env["IMP_WORKTREE_SETUP"] = setup
    env.update(extra_env or {})
    # cwd is NOT the project root: the script must enter it itself.
    return subprocess.run(["bash", str(SCRIPT)], cwd=tmp_path, env=env,
                          capture_output=True, text=True, check=False)


def test_script_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)


def test_configured_setup_runs_in_the_worktree_and_stays_silent(tmp_path: Path) -> None:
    r = _run(tmp_path, "pwd -P > ran-here; echo setup-out; echo setup-err >&2")
    assert r.returncode == 0, r.stderr
    marker = tmp_path / "wt" / "ran-here"
    assert marker.read_text().strip() == str((tmp_path / "wt").resolve())
    assert r.stdout == ""
    assert "setup-out" in r.stderr and "setup-err" in r.stderr
    assert "imp:" not in r.stderr


@pytest.mark.parametrize("setup", [None, ""])
def test_unconfigured_setup_skips(tmp_path: Path, setup: str | None) -> None:
    r = _run(tmp_path, setup)
    assert r.returncode == 0
    assert r.stdout == ""
    assert r.stderr.strip() == "imp: no worktree_setup configured; skipping"


@pytest.mark.parametrize("base_branch,message", [
    (None, "could not read base_branch from quest.json; pausing"),
    ("", "base_branch is empty in quest.json; pausing"),
])
def test_unusable_base_branch_pauses_without_running_setup(
        tmp_path: Path, base_branch: str | None, message: str) -> None:
    r = _run(tmp_path, "touch ran", base_branch=base_branch)
    assert r.returncode == 5
    assert r.stdout == ""
    assert not (tmp_path / "wt" / "ran").exists()
    assert message in r.stderr


def test_failing_setup_pauses_with_the_message_last(tmp_path: Path) -> None:
    cmd = "echo to-stdout; echo to-stderr >&2; exit 3"
    r = _run(tmp_path, cmd)
    assert r.returncode == 3
    assert r.stdout == ""
    lines = r.stderr.splitlines()
    assert "to-stdout" in lines[:-1] and "to-stderr" in lines[:-1]
    assert lines[-1] == f"imp: worktree_setup failed (exit 3) after rebasing onto trunk: {cmd}"


def test_timed_out_setup_pauses_with_the_timeout_message_last(tmp_path: Path) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    fake = fakebin / "timeout"
    fake.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$TIMEOUT_ARGS"\nexit 124\n')
    fake.chmod(0o755)
    args_file = tmp_path / "timeout-args"
    cmd = "sleep 1000"
    r = _run(tmp_path, cmd, {"PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
                             "TIMEOUT_ARGS": str(args_file)})
    assert args_file.read_text().splitlines() == ["-k", "30", "600", "bash", "-c", cmd]
    assert r.returncode == 124
    assert r.stdout == ""
    assert r.stderr.splitlines()[-1] == (
        f"imp: worktree_setup timed out after 600s after rebasing onto trunk: {cmd}")
