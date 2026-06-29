"""imp artifact-commit snapshots .imp/change/artifacts/ to the wip ref."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from imp.cli.main import main
from imp.engine.change import Change
from tests.conftest import _seed_control


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def test_artifact_commit_snapshots_live_tree_to_wip_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as m
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Feat", change_id="feat", base_branch="main")
    art = ch.change_dir / "artifacts" / "spec"
    art.mkdir(parents=True)
    (art / "feat.md").write_text("# the spec\n")

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("IMP_CHANGE_ID", "feat")
    monkeypatch.chdir(ch.project_root)

    rc = main(["artifact-commit", "spec"])
    assert rc == 0
    # The wip tip carries the live spec blob, byte-identical.
    committed = _git(tmp_path, "rev-parse",
                     "refs/imp/artifacts/feat:spec/feat.md").stdout.strip()
    live = _git(tmp_path, "hash-object",
                str(art / "feat.md")).stdout.strip()
    assert committed == live
    # The kind labels the commit message.
    subject = _git(tmp_path, "log", "-1", "--format=%s",
                   "refs/imp/artifacts/feat").stdout.strip()
    assert subject == "artifact: spec"


def test_artifact_commit_fails_when_no_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as m
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Empty", change_id="empty", base_branch="main")
    # No artifacts written — the artifacts dir is absent.

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("IMP_CHANGE_ID", "empty")
    monkeypatch.chdir(ch.project_root)

    rc = main(["artifact-commit", "spec"])
    assert rc == 1


def test_artifact_commit_rejects_review_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as m
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Rev", change_id="rev", base_branch="main")
    art = ch.change_dir / "artifacts" / "review"
    art.mkdir(parents=True)
    (art / "rev.md").write_text("# a review note\n")

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("IMP_CHANGE_ID", "rev")
    monkeypatch.chdir(ch.project_root)

    with pytest.raises(SystemExit) as exc:
        main(["artifact-commit", "review"])
    assert exc.value.code == 2
    assert _git(tmp_path, "rev-parse", "--verify", "--quiet",
                "refs/imp/artifacts/rev").returncode != 0


def test_artifact_commit_accepts_summary_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as m
    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="Feat", change_id="feat", base_branch="main")
    art = ch.change_dir / "artifacts" / "summary"
    art.mkdir(parents=True)
    (art / "feat.md").write_text("# the summary\n")

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("IMP_CHANGE_ID", "feat")
    monkeypatch.chdir(ch.project_root)

    rc = main(["artifact-commit", "summary"])
    assert rc == 0
    # The kind labels the commit message.
    subject = _git(tmp_path, "log", "-1", "--format=%s",
                   "refs/imp/artifacts/feat").stdout.strip()
    assert subject == "artifact: summary"
