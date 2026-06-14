"""base-fresh-check.sh: cheap pre-lock comparison of current base tip to the SHA
integrate-rebase recorded. exit 0 = unchanged (continue), exit 10 = advanced (re-integrate)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path("src/imp/assets/workflows/default/scripts/base-fresh-check.sh").resolve()


def _git(cwd: Path, *a: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=False)


def _seed(pr: Path) -> None:
    pr.mkdir(parents=True, exist_ok=True)
    _git(pr, "init", "-b", "main")
    _git(pr, "config", "user.email", "t@t")
    _git(pr, "config", "user.name", "t")
    (pr / "f.txt").write_text("v1\n")
    _git(pr, "add", "f.txt")
    _git(pr, "commit", "-m", "v1")
    cd = pr / ".imp" / "change"
    cd.mkdir(parents=True)
    (cd / "change.json").write_text(json.dumps({"id": "x", "title": "t", "base_branch": "main"}))


def _run(pr: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=pr.parent,
        env={**os.environ, "IMP_PROJECT_ROOT": str(pr)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_unchanged_base_continues(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr)
    (pr / ".imp" / "change" / "integrated_base_sha").write_text(
        _git(pr, "rev-parse", "main^{commit}").stdout.strip() + "\n"
    )
    assert _run(pr).returncode == 0


def test_advanced_base_signals_reintegrate(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr)
    (pr / ".imp" / "change" / "integrated_base_sha").write_text("0" * 40 + "\n")
    assert _run(pr).returncode == 10


def test_missing_recorded_sha_proceeds_to_merge(tmp_path: Path) -> None:
    # Finding #7: no recorded SHA (e.g. forced rewind to merge that skipped
    # integration) -> proceed to merge directly (exit 0), NOT loop back (exit 10).
    pr = tmp_path / "wt"
    _seed(pr)
    # no integrated_base_sha written
    assert _run(pr).returncode == 0


def test_unresolvable_base_exits_1(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr)
    (pr / ".imp" / "change" / "change.json").write_text(
        json.dumps({"id": "x", "title": "t", "base_branch": "nope"}))
    assert _run(pr).returncode == 1
