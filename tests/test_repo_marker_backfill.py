"""The startup migration that backfills the `repo` ownership marker into imp-owned
state directories, so `_checked_override` has an input to refuse a foreign checkout with."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from imp.cli import config_migrate, state_migrate
from imp.cli.config_migrate import (
    _derive_owner,
    _state_candidates,
    backfill_repo_markers,
    run_startup_migrations,
)
from imp.paths import imp_home, project_state_dir
from tests._dac import dac_enforced
from tests.conftest import _assert_outside_a_git_work_tree, _init_repo

# Every fixture builds real git checkouts; git spawns are slow under a saturated -n auto.
pytestmark = pytest.mark.timeout(240)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args],
                   check=True, capture_output=True, text=True)


def _checkout(parent: Path, name: str) -> Path:
    """A real git checkout — find_control_root only accepts a genuine work tree."""
    root = parent / name
    root.mkdir(parents=True)
    _init_repo(root)
    return root.resolve()


def _state_dir(name: str) -> Path:
    directory = imp_home() / "projects" / name
    directory.mkdir(parents=True)
    return directory


def _quest(state_dir: Path, quest_id: str, project_root: Path | str) -> None:
    home = state_dir / "quests" / quest_id
    home.mkdir(parents=True)
    (home / "quest.json").write_text(json.dumps(
        {"id": quest_id, "title": "t", "base_branch": "main",
         "project_root": str(project_root)}, indent=2, sort_keys=True))


def _record(state_dir: Path, quest_id: str, project_root: Path | str) -> None:
    records = state_dir / "archive" / "records"
    records.mkdir(parents=True, exist_ok=True)
    (records / f"{quest_id}.json").write_text(json.dumps(
        {"id": quest_id, "title": "t",
         "quest_meta": {"id": quest_id, "project_root": str(project_root)}},
        indent=2, sort_keys=True))


def _worktree(state_dir: Path, control: Path, quest_id: str) -> Path:
    path = state_dir / "worktrees" / quest_id
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(control, "worktree", "add", "-q", "-b", f"imp/{quest_id}", str(path), "main")
    return path


def test_candidates_come_from_quest_homes_and_archive_records(tmp_path: Path) -> None:
    """Spec AC 6's source: a project between quests has an archive and no live home."""
    state = _state_dir("widget")
    _quest(state, "aaa111", tmp_path / "from-quest")
    _record(state, "bbb222", tmp_path / "from-archive")

    assert sorted(_state_candidates(state)) == [tmp_path / "from-archive",
                                                tmp_path / "from-quest"]


def test_a_directory_with_no_records_yields_no_candidates() -> None:
    assert _state_candidates(_state_dir("widget")) == []


def test_a_record_without_a_project_root_yields_no_candidate() -> None:
    state = _state_dir("widget")
    (state / "quests" / "aaa111").mkdir(parents=True)
    (state / "quests" / "aaa111" / "quest.json").write_text('{"id": "aaa111"}')

    assert _state_candidates(state) == []


def test_a_malformed_record_raises_for_the_sweep_to_isolate(tmp_path: Path) -> None:
    """Malformed records make the whole DIRECTORY undecidable, so the error is raised
    here and caught per-directory by the sweep (spec AC 8) — never silently ignored."""
    state = _state_dir("widget")
    _quest(state, "aaa111", tmp_path / "somewhere")
    (state / "quests" / "aaa111" / "quest.json").write_text("{not json")

    with pytest.raises(json.JSONDecodeError):
        _state_candidates(state)


def test_a_quest_recording_the_checkout_derives_it(tmp_path: Path) -> None:
    control = _checkout(tmp_path, "widget")
    state = _state_dir("widget")
    _quest(state, "aaa111", control)

    assert _derive_owner(state) == control


def test_an_archive_record_alone_derives_the_owner(tmp_path: Path) -> None:
    """Spec AC 6: an empty quests/ beside one archived record still resolves."""
    control = _checkout(tmp_path, "widget")
    state = _state_dir("widget")
    (state / "quests").mkdir()
    _record(state, "arc111", control)

    assert _derive_owner(state) == control


def test_a_worktree_candidate_normalizes_to_its_control_root(tmp_path: Path) -> None:
    """Spec AC 2: the only candidate on the reporting host is a quest worktree."""
    control = _checkout(tmp_path, "widget")
    state = _state_dir("widget")
    worktree = _worktree(state, control, "aaa111")
    _quest(state, "aaa111", worktree)

    assert worktree != control
    assert _derive_owner(state) == control


def test_a_vanished_worktree_candidate_drops_alone(tmp_path: Path) -> None:
    """Spec AC 3: a dead candidate neither vetoes the survivors nor breaks unanimity."""
    control = _checkout(tmp_path, "widget")
    state = _state_dir("widget")
    _quest(state, "aaa111", state / "worktrees" / "aaa111")   # never created
    _quest(state, "bbb222", control)

    assert _derive_owner(state) == control


def test_a_worktree_with_no_resolvable_git_dir_drops_alone(tmp_path: Path) -> None:
    """Spec AC 3, the PreconditionError half: the scratch repo was deleted out from
    under a leaked quest's worktree, leaving a plain directory behind."""
    control = _checkout(tmp_path, "widget")
    state = _state_dir("widget")
    orphan = state / "worktrees" / "ccc333"
    orphan.mkdir(parents=True)
    _quest(state, "ccc333", orphan)
    _quest(state, "bbb222", control)

    assert _derive_owner(state) == control


def test_a_scratch_checkout_the_walk_could_not_have_minted_is_rejected(
    tmp_path: Path,
) -> None:
    """Spec AC 4: the vave28 shape pointed at the migration itself — a leaked quest
    records /tmp/spike/r1, which slugs to `r1` and can never own projects/widget."""
    scratch = _checkout(tmp_path / "spike", "r1")
    state = _state_dir("widget")
    _quest(state, "aaa111", scratch)

    assert _derive_owner(state) is None


@pytest.mark.parametrize("dir_name", ["widget", "widget-2", "widget-10"])
def test_the_mintable_names_are_admitted(tmp_path: Path, dir_name: str) -> None:
    """Spec AC 5: the anti-poison clause must not break normal collision handling."""
    control = _checkout(tmp_path, "widget")
    state = _state_dir(dir_name)
    _quest(state, "aaa111", control)

    assert _derive_owner(state) == control


@pytest.mark.parametrize("dir_name", ["widget-1", "widget-x", "widgets", "gadget"])
def test_a_name_the_mint_rule_could_not_produce_is_rejected(
    tmp_path: Path, dir_name: str,
) -> None:
    """`-1` is never minted: n == 1 is the bare slug."""
    control = _checkout(tmp_path, "widget")
    state = _state_dir(dir_name)
    _quest(state, "aaa111", control)

    assert _derive_owner(state) is None


def test_a_candidate_that_is_not_a_checkout_is_dropped(tmp_path: Path) -> None:
    """Spec AC 7c."""
    plain = tmp_path / "widget"
    plain.mkdir()
    state = _state_dir("widget")
    _quest(state, "aaa111", plain)

    assert _derive_owner(state) is None


def test_a_candidate_below_the_checkout_root_is_dropped(tmp_path: Path) -> None:
    """Spec AC 7c: inside a checkout but not the checkout, and not under worktrees/."""
    control = _checkout(tmp_path, "widget")
    subdir = control / "src"
    subdir.mkdir()
    state = _state_dir("widget")
    _quest(state, "aaa111", subdir)

    assert _derive_owner(state) is None


def test_a_symlinked_project_root_still_derives_the_owner(tmp_path: Path) -> None:
    """The comparison is resolved, so a symlinked recorded root is not silently lost."""
    control = _checkout(tmp_path / "real", "widget")
    link = tmp_path / "link"
    link.symlink_to(control)
    state = _state_dir("widget")
    _quest(state, "aaa111", link)

    assert _derive_owner(state) == control


def test_two_surviving_owners_leave_it_underivable(tmp_path: Path) -> None:
    """Spec AC 7b: the migration never guesses."""
    first = _checkout(tmp_path / "a", "widget")
    second = _checkout(tmp_path / "b", "widget")
    state = _state_dir("widget")
    _quest(state, "aaa111", first)
    _record(state, "bbb222", second)

    assert _derive_owner(state) is None


def test_no_candidates_leave_it_underivable() -> None:
    """Spec AC 7a."""
    assert _derive_owner(_state_dir("widget")) is None


def test_a_repeated_candidate_costs_one_git_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Derivation re-runs on every invocation for a directory that stays unmarked
    (the AC-14 deferral, and every sandboxed call until the marker is re-exposed), so
    a project whose N records all name one root must not pay N `git rev-parse` spawns."""
    from imp.engine import worktree

    control = _checkout(tmp_path, "widget")
    state = _state_dir("widget")
    for quest_id in ("aaa111", "bbb222", "ccc333"):
        _quest(state, quest_id, control)
    _record(state, "ddd444", control)
    seen: list[Path] = []
    real = worktree.find_control_root

    def counting(start: Path) -> Path:
        seen.append(start)
        return real(start)

    monkeypatch.setattr(worktree, "find_control_root", counting)

    assert _derive_owner(state) == control
    assert seen == [control]


def test_the_sweep_writes_the_resolved_owner_with_a_trailing_newline(
    tmp_path: Path,
) -> None:
    """Spec AC 1: byte-identical to the slug walk's own mint form (paths.py:61)."""
    control = _checkout(tmp_path, "widget")
    state = _state_dir("widget")
    _quest(state, "aaa111", control)

    backfill_repo_markers()

    assert (state / "repo").read_text() == f"{control.resolve()}\n"


def test_the_sweep_resolves_a_symlinked_project_root_before_writing(
    tmp_path: Path,
) -> None:
    """Resolution is load-bearing: paths.py:53 and :71 compare against str(root.resolve())."""
    control = _checkout(tmp_path / "real", "widget")
    link = tmp_path / "link"
    link.symlink_to(control)
    state = _state_dir("widget")
    _quest(state, "aaa111", link)

    backfill_repo_markers()

    assert (state / "repo").read_text() == f"{control}\n"


def test_a_backfilled_directory_is_returned_by_the_slug_walk(tmp_path: Path) -> None:
    """Spec AC 12: without the marker the walk skips it and mints `widget-2`."""
    control = _checkout(tmp_path, "widget")
    state = _state_dir("widget")
    _quest(state, "aaa111", control)

    backfill_repo_markers()

    assert project_state_dir(control) == state
    assert not (imp_home() / "projects" / "widget-2").exists()


def test_a_directory_with_no_derivable_owner_is_left_unmarked() -> None:
    """Spec AC 7a."""
    state = _state_dir("widget")

    backfill_repo_markers()

    assert not (state / "repo").exists()


def test_a_leaked_scratch_quest_cannot_poison_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec AC 4 — the vave28 shape pointed at the migration itself. (a) nothing is
    written, so (b) the genuine owner is still accepted by the override guard rather
    than locked out behind a marker naming the scratch checkout."""
    genuine = _checkout(tmp_path / "home", "widget")
    scratch = _checkout(tmp_path / "spike", "r1")
    state = _state_dir("widget")
    _quest(state, "aaa111", scratch)

    backfill_repo_markers()

    assert not (state / "repo").exists()
    monkeypatch.setenv("IMP_STATE_DIR", str(state))
    assert project_state_dir(genuine) == state


def test_an_existing_marker_is_left_byte_identical(tmp_path: Path) -> None:
    """Spec AC 9: presence of the marker is the guard, even when it names another
    checkout — repointing one is the operator's call, not the migration's."""
    control = _checkout(tmp_path, "widget")
    other = tmp_path / "elsewhere" / "widget"
    state = _state_dir("widget")
    _quest(state, "aaa111", control)
    (state / "repo").write_text(f"{other}\n")

    backfill_repo_markers()

    assert (state / "repo").read_text() == f"{other}\n"


def test_a_malformed_and_an_unwritable_directory_do_not_abort_the_sweep(
    tmp_path: Path,
) -> None:
    """Spec AC 8: both failures sort BEFORE the good directory, so a sweep that
    aborted on either would leave `gamma` unmarked. The unwritable case is routine:
    in-sandbox `imp_home()/projects` is a read-only bind."""
    alpha = _checkout(tmp_path, "alpha")
    beta = _checkout(tmp_path, "beta")
    gamma = _checkout(tmp_path, "gamma")
    alpha_state = _state_dir("alpha")
    _quest(alpha_state, "aaa111", alpha)
    (alpha_state / "quests" / "aaa111" / "quest.json").write_text("{not json")
    beta_state = _state_dir("beta")
    _quest(beta_state, "bbb222", beta)
    gamma_state = _state_dir("gamma")
    _quest(gamma_state, "ccc333", gamma)
    beta_state.chmod(0o500)
    try:
        # The suite runs as uid 0 in a userns holding CAP_DAC_OVERRIDE, where a write
        # into an r-x directory silently succeeds; dropping it is what makes the
        # unwritable half real. r-x still lets _derive_owner read beta's records.
        with dac_enforced():
            backfill_repo_markers()
    finally:
        beta_state.chmod(0o700)

    assert (gamma_state / "repo").read_text() == f"{gamma}\n"
    assert not (alpha_state / "repo").exists()
    assert not (beta_state / "repo").exists()


@pytest.mark.parametrize("stub_shape", ["bare", "empty-quests"])
def test_a_marker_only_stub_sibling_does_not_block_the_repair(
    tmp_path: Path, stub_shape: str,
) -> None:
    """Spec AC 13 — the reporting host's shape. Blocking on a contentless stub would
    leave the live directory unmarked and the whole fix ceremonial there."""
    control = _checkout(tmp_path, "widget")
    live = _state_dir("widget")
    _quest(live, "aaa111", control)
    stub = _state_dir("widget-2")
    (stub / "repo").write_text(f"{control}\n")
    if stub_shape == "empty-quests":
        (stub / "quests").mkdir()

    backfill_repo_markers()

    assert (live / "repo").read_text() == f"{control}\n"
    assert project_state_dir(control) == live          # the stub is retired, not deleted
    assert (stub / "repo").read_text() == f"{control}\n"


@pytest.mark.parametrize("sibling_shape", ["quests", "archive"])
def test_a_sibling_holding_real_state_keeps_the_claim(
    tmp_path: Path, sibling_shape: str,
) -> None:
    """Spec AC 14: `<slug>` outranks `<slug>-2` in the walk, so marking a stale
    `<slug>` would steal resolution from the live sibling and orphan its quests."""
    control = _checkout(tmp_path, "widget")
    stale = _state_dir("widget")
    _quest(stale, "aaa111", control)
    live = _state_dir("widget-2")
    (live / "repo").write_text(f"{control}\n")
    if sibling_shape == "quests":
        _quest(live, "bbb222", control)
    else:
        (live / "archive").mkdir()

    backfill_repo_markers()

    assert not (stale / "repo").exists()
    assert project_state_dir(control) == live


def test_a_sibling_claiming_another_owner_does_not_block(tmp_path: Path) -> None:
    """Only a claim on the DERIVED owner defers the backfill."""
    control = _checkout(tmp_path / "a", "widget")
    other = _checkout(tmp_path / "b", "widget")
    state = _state_dir("widget")
    _quest(state, "aaa111", control)
    sibling = _state_dir("widget-2")
    (sibling / "repo").write_text(f"{other}\n")
    _quest(sibling, "bbb222", other)

    backfill_repo_markers()

    assert (state / "repo").read_text() == f"{control}\n"


def test_a_marker_written_this_pass_does_not_block_a_later_directory(
    tmp_path: Path,
) -> None:
    """Spec AC 15: siblings are read once up front, so the outcome cannot depend on
    visit order. An implementation that re-read the tree per directory would mark
    only whichever of these two it happened to reach first."""
    control = _checkout(tmp_path, "widget")
    first = _state_dir("widget")
    _quest(first, "aaa111", control)
    second = _state_dir("widget-2")
    _quest(second, "bbb222", control)

    backfill_repo_markers()

    assert (first / "repo").read_text() == f"{control}\n"
    assert (second / "repo").read_text() == f"{control}\n"


def test_startup_performs_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec AC 10a."""
    monkeypatch.chdir(tmp_path)   # never resolve the state step against the real checkout
    _assert_outside_a_git_work_tree(tmp_path)
    control = _checkout(tmp_path, "widget")
    state = _state_dir("widget")
    _quest(state, "aaa111", control)

    run_startup_migrations()

    assert (state / "repo").read_text() == f"{control}\n"


def test_a_failing_sweep_does_not_kill_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec AC 10b: best-effort like every config step — retried next invocation."""
    monkeypatch.chdir(tmp_path)
    _assert_outside_a_git_work_tree(tmp_path)

    def boom() -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(config_migrate, "backfill_repo_markers", boom)

    run_startup_migrations()   # must not raise


def test_the_sweep_runs_before_the_state_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec AC 10c: the state move mints through project_state_dir, so a backfill
    wired after it would let a legacy layout split into `<slug>-2` first."""
    monkeypatch.chdir(tmp_path)
    _assert_outside_a_git_work_tree(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(config_migrate, "backfill_repo_markers",
                        lambda: calls.append("backfill"))
    monkeypatch.setattr(state_migrate, "migrate_project_state",
                        lambda _cwd: calls.append("state"))

    run_startup_migrations()

    assert calls == ["backfill", "state"]


def test_worktree_candidate_with_symlinked_imp_home_is_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worktree clause fires even when IMP_HOME traverses a symlink (/home -> /var/home)."""
    real = tmp_path / "real" / "imphome"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real")
    monkeypatch.setenv("IMP_HOME", str(link / "imphome"))

    control = _checkout(tmp_path, "widget")
    state = _state_dir("widget")
    worktree = _worktree(state, control, "aaa111")
    _quest(state, "aaa111", worktree.resolve())

    backfill_repo_markers()

    assert (state / "repo").read_text() == f"{control}\n"


def test_a_directory_outside_projects_is_never_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec AC 11: a hand-pinned $IMP_STATE_DIR stays unmarked, so
    architecture.md's "no marker written" promise holds."""
    monkeypatch.chdir(tmp_path)
    _assert_outside_a_git_work_tree(tmp_path)
    control = _checkout(tmp_path, "widget")
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    _quest(pinned, "aaa111", control)
    monkeypatch.setenv("IMP_STATE_DIR", str(pinned))

    run_startup_migrations()

    assert not (pinned / "repo").exists()
