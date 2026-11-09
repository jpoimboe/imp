"""The project state-directory seam: slug claiming and the IMP_STATE_DIR override.

IMP_HOME is isolated per test by conftest's `_isolate_global_config`, so claiming
resolves under each test's own tmp_path.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from imp import paths
from imp.engine.worktree import PreconditionError, find_control_root
from imp.paths import (
    claimed_state_dir,
    display_path,
    imp_home,
    project_state_dir,
    resolve_state_dir,
)
from tests.conftest import _git, _init_repo


def _repo(parent: Path, name: str) -> Path:
    root = parent / name
    root.mkdir(parents=True)
    return root


def test_claim_creates_the_slug_dir_and_writes_the_repo_marker(tmp_path: Path) -> None:
    root = _repo(tmp_path, "widget")
    state = project_state_dir(root)
    assert state == imp_home() / "projects" / "widget"
    assert state.is_dir()
    assert (state / "repo").read_text() == f"{root.resolve()}\n"


def test_reresolving_the_same_root_returns_the_same_directory(tmp_path: Path) -> None:
    root = _repo(tmp_path, "widget")
    first = project_state_dir(root)
    second = project_state_dir(root)
    assert second == first
    assert [p.name for p in (imp_home() / "projects").iterdir()] == ["widget"]


@pytest.mark.parametrize(
    ("basename", "slug"),
    [
        ("Widget Co", "widget-co"),
        ("My.Repo_1", "my.repo_1"),
        ("Ünïcode~repo", "-n-code-repo"),
        ("a" * 70, "a" * 64),
    ],
)
def test_slug_sanitizes_the_basename(tmp_path: Path, basename: str, slug: str) -> None:
    root = _repo(tmp_path, basename)
    assert project_state_dir(root) == imp_home() / "projects" / slug


def test_a_root_with_no_basename_slugs_to_project() -> None:
    assert project_state_dir(Path("/")) == imp_home() / "projects" / "project"


def test_same_basename_in_other_checkouts_claims_numbered_slugs(tmp_path: Path) -> None:
    first = _repo(tmp_path / "a", "widget")
    second = _repo(tmp_path / "b", "widget")
    third = _repo(tmp_path / "c", "widget")
    assert project_state_dir(first).name == "widget"
    assert project_state_dir(second).name == "widget-2"
    assert project_state_dir(third).name == "widget-3"
    # Each root keeps resolving to its own claim.
    assert project_state_dir(second).name == "widget-2"
    assert (imp_home() / "projects" / "widget-2" / "repo").read_text() == (
        f"{second.resolve()}\n"
    )


def test_a_candidate_without_a_marker_is_skipped_not_adopted(tmp_path: Path) -> None:
    squatter = imp_home() / "projects" / "widget"
    squatter.mkdir(parents=True)
    root = _repo(tmp_path, "widget")
    state = project_state_dir(root)
    assert state == imp_home() / "projects" / "widget-2"
    assert not (squatter / "repo").exists()


def test_a_candidate_with_a_foreign_marker_is_skipped(tmp_path: Path) -> None:
    taken = imp_home() / "projects" / "widget"
    taken.mkdir(parents=True)
    (taken / "repo").write_text(f"{tmp_path / 'somewhere' / 'widget'}\n")
    root = _repo(tmp_path, "widget")
    assert project_state_dir(root) == imp_home() / "projects" / "widget-2"
    assert (taken / "repo").read_text() == f"{tmp_path / 'somewhere' / 'widget'}\n"


def test_override_is_returned_verbatim_and_claims_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    override = tmp_path / "elsewhere" / "state"
    monkeypatch.setenv("IMP_STATE_DIR", str(override))
    root = _repo(tmp_path, "widget")
    assert project_state_dir(root) == override
    assert not override.exists()
    assert not (imp_home() / "projects").exists()


def test_override_marked_for_this_checkout_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path, "widget")
    override = tmp_path / "state"
    override.mkdir()
    (override / "repo").write_text(f"{root.resolve()}\n")
    monkeypatch.setenv("IMP_STATE_DIR", str(override))
    assert project_state_dir(root) == override


def test_override_without_a_marker_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not the in-sandbox shape any more (the mask re-exposes the marker): this is the
    # hand-pinned $IMP_STATE_DIR, and the legacy _pin window, which carry no marker.
    override = tmp_path / "state"
    override.mkdir()
    monkeypatch.setenv("IMP_STATE_DIR", str(override))
    root = _repo(tmp_path, "widget")
    assert project_state_dir(root) == override
    assert not (imp_home() / "projects").exists()


def test_override_holding_another_checkouts_state_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = tmp_path / "other" / "repo"
    override = tmp_path / "state"
    override.mkdir()
    (override / "repo").write_text(f"{other}\n")
    monkeypatch.setenv("IMP_STATE_DIR", str(override))
    root = _repo(tmp_path, "widget")
    with pytest.raises(PreconditionError) as excinfo:
        project_state_dir(root)
    assert str(excinfo.value) == (
        f"IMP_STATE_DIR={display_path(override)} holds state for "
        f"{display_path(other)} — refusing to use it here. Fix: to run imp against a "
        "scratch checkout, unset IMP_STATE_DIR and point IMP_HOME at a throwaway "
        "directory."
    )


def test_the_guard_docstring_no_longer_calls_an_absent_marker_the_sandbox_shape() -> None:
    # The sandbox re-exposes <state dir>/repo read-only through the state tmpfs, so an
    # absent marker no longer means "in-sandbox" — it means a hand-pinned state dir, which
    # is unguarded by choice. The docstring is the only place that rule is written down.
    doc = paths._checked_override.__doc__ or ""
    assert "masked in-sandbox shape" not in doc
    assert "hand-pinned" in doc
    assert "read-only" in doc


def test_a_bare_probe_root_is_refused_not_handed_the_live_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC 17 — the archive-probe shape: an agent that resolves state from a throwaway
    # directory must be refused, not handed the live project's state dir to write into.
    root = _repo(tmp_path, "widget")
    override = tmp_path / "state"
    override.mkdir()
    (override / "repo").write_text(f"{root.resolve()}\n")
    monkeypatch.setenv("IMP_STATE_DIR", str(override))
    probe = Path(tempfile.mkdtemp(dir=tmp_path))
    with pytest.raises(PreconditionError):
        project_state_dir(probe)


def test_the_marked_owner_resolves_from_its_checkout_and_from_a_linked_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC 18: every legitimate imp run happens either in the main checkout or in a quest
    # worktree, and both must keep resolving — the guard keys on the control root, which
    # git's common dir maps a linked worktree back onto.
    root = _repo(tmp_path, "widget")
    _init_repo(root)
    linked = tmp_path / "linked"
    _git(root, "worktree", "add", "-q", "-b", "feat", str(linked), "main")
    override = tmp_path / "state"
    override.mkdir()
    (override / "repo").write_text(f"{root.resolve()}\n")
    monkeypatch.setenv("IMP_STATE_DIR", str(override))
    for cwd in (root, linked):
        assert find_control_root(cwd) == root.resolve()
        assert project_state_dir(find_control_root(cwd)) == override


def test_claimed_state_dir_returns_none_for_an_unclaimed_root_and_creates_nothing(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path, "widget")
    assert claimed_state_dir(root) is None
    assert not (imp_home() / "projects").exists()


def test_claimed_state_dir_returns_the_directory_after_project_state_dir_claims_it(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path, "widget")
    state = project_state_dir(root)
    assert claimed_state_dir(root) == state


def test_claimed_state_dir_returns_the_override_verbatim_when_imp_state_dir_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    override = tmp_path / "elsewhere" / "state"
    monkeypatch.setenv("IMP_STATE_DIR", str(override))
    root = _repo(tmp_path, "widget")
    assert claimed_state_dir(root) == override
    assert not override.exists()


def test_resolve_state_dir_names_the_dir_a_claim_would_pick_and_creates_nothing(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path, "widget")
    assert resolve_state_dir(root) == (imp_home() / "projects" / "widget", False)
    assert not (imp_home() / "projects").exists()
    state = project_state_dir(root)
    assert state == imp_home() / "projects" / "widget"
    assert (state / "repo").read_text() == f"{root.resolve()}\n"


def test_resolve_state_dir_skips_a_slug_another_root_owns(tmp_path: Path) -> None:
    taken = imp_home() / "projects" / "widget"
    taken.mkdir(parents=True)
    (taken / "repo").write_text(f"{tmp_path / 'somewhere' / 'widget'}\n")
    root = _repo(tmp_path, "widget")
    assert resolve_state_dir(root) == (imp_home() / "projects" / "widget-2", False)
    assert project_state_dir(root) == imp_home() / "projects" / "widget-2"


def test_resolve_state_dir_reports_a_claimed_dir_as_claimed(tmp_path: Path) -> None:
    root = _repo(tmp_path, "widget")
    state = project_state_dir(root)
    assert resolve_state_dir(root) == (state, True)


def test_resolve_state_dir_reads_a_missing_override_as_unclaimed_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    override = tmp_path / "elsewhere" / "state"
    monkeypatch.setenv("IMP_STATE_DIR", str(override))
    root = _repo(tmp_path, "widget")
    assert resolve_state_dir(root) == (override, False)
    assert project_state_dir(root) == override
    assert not override.exists()
    assert not (imp_home() / "projects").exists()
