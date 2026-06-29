"""The engine harvests change_dir/artifacts/** to refs/imp/artifacts/<id>."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from imp.engine import executor


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")


def _change(root: Path, change_id: str) -> SimpleNamespace:
    # The harvest helper only reads .control_root, .change_dir and .id.
    return SimpleNamespace(
        control_root=root,
        change_dir=root / ".imp" / "changes" / change_id,
        id=change_id,
    )


def test_harvest_journals_disk_artifacts_and_guards_redundant_commits(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    ch = _change(tmp_path, "feat")
    spec = ch.change_dir / "artifacts" / "spec"
    spec.mkdir(parents=True)
    (spec / "feat.md").write_text("# spec\n")

    executor._harvest_artifacts(ch, SimpleNamespace(name="spec-finalizer"))
    assert _git(tmp_path, "show",
                "refs/imp/artifacts/feat:spec/feat.md").stdout == "# spec\n"
    # Commit message derives from the step name (no hardcoded kind).
    assert _git(tmp_path, "log", "-1", "--format=%s",
                "refs/imp/artifacts/feat").stdout.strip() == "artifact: spec-finalizer"
    head1 = _git(tmp_path, "rev-parse", "refs/imp/artifacts/feat").stdout.strip()

    # A later step that did not change artifacts/ adds NO new journal commit.
    executor._harvest_artifacts(ch, SimpleNamespace(name="plan-reviewer"))
    assert _git(tmp_path, "rev-parse",
                "refs/imp/artifacts/feat").stdout.strip() == head1


def test_harvest_is_noop_when_artifacts_dir_absent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    ch = _change(tmp_path, "bare")
    # No artifacts/ dir exists (e.g. an early branch step).
    executor._harvest_artifacts(ch, SimpleNamespace(name="branch"))
    assert _git(tmp_path, "rev-parse", "--verify", "--quiet",
                "refs/imp/artifacts/bare").returncode != 0
