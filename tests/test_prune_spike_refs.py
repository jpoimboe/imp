"""The one-shot sweep that drops parked spike evidence out of the artifact refs."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from imp.engine import worktree as wt
from tests.conftest import _init_repo


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    return root


def _park(root: Path, quest_id: str, files: dict[str, str], *, message: str,
          when: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """One artifact snapshot through the production path. Author, committer and the
    repo's own `t <t@t>` config are three DISTINCT identities, and the two dates differ,
    so no preserved field can be reproduced by a git default. The vars are cleared again
    before returning — left set, they would leak into the sweep's own environment and
    reproduce the identities there by accident, hiding a replay that drops them."""
    ident = {
        "GIT_AUTHOR_NAME": "spiker", "GIT_AUTHOR_EMAIL": "spiker@imp",
        "GIT_AUTHOR_DATE": f"{when}T03:04:05+02:00",
        "GIT_COMMITTER_NAME": "harvester", "GIT_COMMITTER_EMAIL": "harvester@imp",
        "GIT_COMMITTER_DATE": f"{when}T09:10:11-07:00",
    }
    for name, value in ident.items():
        monkeypatch.setenv(name, value)
    sources = {}
    for name, body in files.items():
        src = root.parent / "files" / quest_id / name
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(body)
        sources[name] = src
    assert wt.artifact_commit(root, quest_id, sources, message=message) is True
    for name in ident:
        monkeypatch.delenv(name)


_FIELDS = "--format=%s%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%aI%x1f%cI"


def _chain(root: Path, quest_id: str) -> list[str]:
    return _git(root, "log", _FIELDS, f"refs/imp/artifacts/{quest_id}").splitlines()


def _seed_spiked(root: Path, quest_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Four snapshots: spike-free, then evidence alone (the threshold-step shape, whose
    tree reduces to nothing), then evidence beside a spec, then a plan too."""
    _park(root, quest_id, {"spec/f.md": "# spec\n"},
          message="artifacts: threshold", when="2026-01-02", monkeypatch=monkeypatch)
    _park(root, quest_id, {"spike/probe.py": "print(1)\n"},
          message="artifacts: spiker", when="2026-01-03", monkeypatch=monkeypatch)
    _park(root, quest_id, {"spec/f.md": "# spec\n", "spike/probe.py": "print(1)\n"},
          message="artifacts: spec-finalizer", when="2026-01-04", monkeypatch=monkeypatch)
    _park(root, quest_id,
          {"spec/f.md": "# spec\n", "spike/probe.py": "print(1)\n",
           "plans/p1.md": "# plan 1\n"},
          message="artifacts: plan-writer", when="2026-01-05", monkeypatch=monkeypatch)


def test_the_sweep_drops_spike_and_leaves_every_other_field_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    _seed_spiked(root, "aaa", monkeypatch)
    ref = "refs/imp/artifacts/aaa"
    before_chain = _chain(root, "aaa")
    before_spec = _git(root, "rev-parse", f"{ref}:spec/f.md").strip()
    before_root = _git(root, "rev-parse", f"{ref}~3").strip()

    assert wt.prune_spike_from_artifact_refs(root) == 1

    assert _git(root, "ls-tree", "--name-only", ref).split() == ["plans", "spec"]
    assert _git(root, "rev-list", ref, "--", "spike").strip() == ""
    assert len(_git(root, "rev-list", ref).split()) == 4
    assert _chain(root, "aaa") == before_chain
    assert _git(root, "show", f"{ref}:spec/f.md") == "# spec\n"
    assert _git(root, "show", f"{ref}:plans/p1.md") == "# plan 1\n"
    assert _git(root, "rev-parse", f"{ref}:spec/f.md").strip() == before_spec
    # The spike-only snapshot reduces to an empty tree rather than dropping out.
    assert _git(root, "ls-tree", "--name-only", f"{ref}~2").split() == []
    # The spike-free root commit re-hashes to itself, so lineage below the evidence
    # is not merely equivalent — it is the same object.
    assert _git(root, "rev-parse", f"{ref}~3").strip() == before_root


def test_a_spike_free_ref_keeps_its_exact_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    _park(root, "bbb", {"spec/g.md": "# g\n"},
          message="artifacts: threshold", when="2026-02-02", monkeypatch=monkeypatch)
    _park(root, "bbb", {"spec/g.md": "# g\n", "plans/p1.md": "# plan\n"},
          message="artifacts: plan-writer", when="2026-02-03", monkeypatch=monkeypatch)
    before = _git(root, "rev-parse", "refs/imp/artifacts/bbb").strip()

    assert wt.prune_spike_from_artifact_refs(root) == 0

    assert _git(root, "rev-parse", "refs/imp/artifacts/bbb").strip() == before


def test_a_second_sweep_rewrites_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    _seed_spiked(root, "aaa", monkeypatch)
    assert wt.prune_spike_from_artifact_refs(root) == 1
    swept = _git(root, "rev-parse", "refs/imp/artifacts/aaa").strip()

    assert wt.prune_spike_from_artifact_refs(root) == 0

    assert _git(root, "rev-parse", "refs/imp/artifacts/aaa").strip() == swept


def test_a_ref_failing_mid_rewrite_is_skipped_and_the_next_is_still_swept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    _seed_spiked(root, "aaa", monkeypatch)
    _park(root, "zzz", {"spec/g.md": "# g\n", "spike/probe.py": "print(2)\n"},
          message="artifacts: threshold", when="2026-03-02", monkeypatch=monkeypatch)
    before_aaa = _git(root, "rev-parse", "refs/imp/artifacts/aaa").strip()
    real = wt._replay_without_spike
    seen: list[str] = []

    def flaky(control_root: Path, commit: str, parent: str) -> str:
        seen.append(commit)
        if len(seen) == 2:                     # part-way through the first ref's chain
            raise RuntimeError("object write failed")
        return real(control_root, commit, parent)

    monkeypatch.setattr(wt, "_replay_without_spike", flaky)

    assert wt.prune_spike_from_artifact_refs(root) == 1

    assert _git(root, "rev-parse", "refs/imp/artifacts/aaa").strip() == before_aaa
    assert _git(root, "rev-list", "refs/imp/artifacts/aaa", "--", "spike").strip() != ""
    assert _git(root, "ls-tree", "--name-only",
                "refs/imp/artifacts/zzz").split() == ["spec"]


def test_a_repo_with_no_artifact_refs_sweeps_nothing(tmp_path: Path) -> None:
    assert wt.prune_spike_from_artifact_refs(_repo(tmp_path)) == 0
