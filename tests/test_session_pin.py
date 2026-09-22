from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

import imp
import imp.remote.session.pin as pin_mod
from imp.engine.worktree import RepoGitLock
from imp.paths import SOURCE_ROOT_MARKER
from imp.remote.session.pin import (
    OWNER_PID_MARKER,
    PINNED_SRC_DIRNAME,
    pin_source_snapshot,
)
from tests.async_wait import HANG_BACKSTOP


def _fake_source(root: Path, *, marker: str = "one") -> Path:
    """A src/ shaped like the two packages pyproject ships — small enough to copy in a
    test — each carrying a __pycache__ that must not travel into the snapshot."""
    src = root / "src"
    for pkg in ("imp", "impty"):
        (src / pkg / "__pycache__").mkdir(parents=True)
        (src / pkg / "__init__.py").write_text(f"{pkg} {marker}\n")
        (src / pkg / "__pycache__" / "__init__.cpython-311.pyc").write_bytes(b"\x00stale")
    (src / "imp" / "assets").mkdir()
    (src / "imp" / "assets" / "prompt.md").write_text("# asset\n")
    return src


def _tree(root: Path) -> dict[str, bytes]:
    """Relative path -> bytes for every file under root, __pycache__ excluded."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.relative_to(root).parts
    }


def test_a_published_snapshot_is_a_faithful_copy_of_the_running_packages(
        tmp_path: Path) -> None:
    """The in-sandbox imp runs out of this directory, so a snapshot that drops the
    non-.py prompt/skill corpus or the lazily imported impty package is not a runnable
    imp. Compared against the running packages, file for file."""
    quest_dir = tmp_path / "quest"

    snapshot = pin_source_snapshot(quest_dir, tmp_path / "control")

    running_src = Path(imp.__file__).resolve().parent.parent
    assert snapshot.parent == quest_dir / PINNED_SRC_DIRNAME
    assert (snapshot / "imp" / "__init__.py").is_file()
    assert _tree(snapshot / "imp") == _tree(running_src / "imp")
    assert _tree(snapshot / "impty") == _tree(running_src / "impty")
    assert (snapshot / "imp" / "assets" / "skills" / "imp-knowledge.md").is_file()
    assert (snapshot / SOURCE_ROOT_MARKER).read_text().strip() == str(running_src)
    assert (snapshot / OWNER_PID_MARKER).read_text().strip() == str(os.getpid())


def test_a_snapshot_carries_no_bytecode_cache(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """__pycache__ is the one exclusion: stale bytecode keyed to another interpreter
    has no business in a frozen source tree."""
    src = _fake_source(tmp_path)
    monkeypatch.setattr(imp, "__file__", str(src / "imp" / "__init__.py"))

    snapshot = pin_source_snapshot(tmp_path / "quest", tmp_path / "control")

    assert list(src.rglob("__pycache__")) != []          # the source has caches to drop
    assert list(snapshot.rglob("__pycache__")) == []
    assert (snapshot / "imp" / "__init__.py").read_text() == "imp one\n"
    assert (snapshot / "impty" / "__init__.py").read_text() == "impty one\n"
    assert (snapshot / "imp" / "assets" / "prompt.md").read_text() == "# asset\n"


def test_a_successful_pin_leaves_no_temporary_directory(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The snapshot is built aside and published by rename, so the build directory is
    gone by construction rather than by a cleanup that could be skipped."""
    src = _fake_source(tmp_path)
    monkeypatch.setattr(imp, "__file__", str(src / "imp" / "__init__.py"))
    quest_dir = tmp_path / "quest"

    pin_source_snapshot(quest_dir, tmp_path / "control")

    assert [p.name for p in quest_dir.iterdir()] == [PINNED_SRC_DIRNAME]


def test_a_copy_that_fails_part_way_publishes_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A half-copied tree must never become visible under pinned-src/: a concurrent
    daemon reads every published directory as a complete, owned snapshot."""
    src = _fake_source(tmp_path)
    monkeypatch.setattr(imp, "__file__", str(src / "imp" / "__init__.py"))
    quest_dir = tmp_path / "quest"
    real_copytree = shutil.copytree
    copied: list[str] = []

    def flaky(*args: Any, **kwargs: Any) -> Any:
        copied.append(str(args[1]))
        if len(copied) == 2:                 # imp/ lands, impty/ blows up
            raise OSError("disk full")
        return real_copytree(*args, **kwargs)

    monkeypatch.setattr(pin_mod.shutil, "copytree", flaky)

    with pytest.raises(OSError, match="disk full"):
        pin_source_snapshot(quest_dir, tmp_path / "control")

    assert not (quest_dir / PINNED_SRC_DIRNAME).exists()
    assert [p for p in quest_dir.iterdir() if p.name.startswith("pinning-")] == []


def test_a_missing_impty_beside_the_running_imp_is_a_pin_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both packages ship in every install, so a missing impty/ is a broken source
    tree, not a configuration to tolerate — the daemon lazily imports impty mid-run."""
    src = _fake_source(tmp_path)
    shutil.rmtree(src / "impty")
    monkeypatch.setattr(imp, "__file__", str(src / "imp" / "__init__.py"))
    quest_dir = tmp_path / "quest"

    with pytest.raises(OSError) as excinfo:
        pin_source_snapshot(quest_dir, tmp_path / "control")

    assert "impty" in str(excinfo.value)
    assert not (quest_dir / PINNED_SRC_DIRNAME).exists()


def test_the_copy_waits_for_the_repo_lock(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """merge_to_base takes this same lock to move the base branch, so holding it across
    the copy is what makes a merge and a snapshot unable to interleave."""
    src = _fake_source(tmp_path)
    monkeypatch.setattr(imp, "__file__", str(src / "imp" / "__init__.py"))
    quest_dir = tmp_path / "quest"
    control_root = tmp_path / "control"
    copy_started = threading.Event()
    finished = threading.Event()
    published: list[Path] = []
    errors: list[BaseException] = []
    real_copytree = shutil.copytree

    def watched(*args: Any, **kwargs: Any) -> Any:
        copy_started.set()
        return real_copytree(*args, **kwargs)

    monkeypatch.setattr(pin_mod.shutil, "copytree", watched)

    def run_pin() -> None:
        try:
            published.append(pin_source_snapshot(quest_dir, control_root))
        except BaseException as exc:                     # surfaced by the assert below
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=run_pin)
    with RepoGitLock(control_root).acquire():
        worker.start()
        # Negative direction only: a window this side of the lock release, not a tuned
        # wait for success — the copy must not have started at all.
        assert not copy_started.wait(0.5)
        assert not finished.is_set()
    assert finished.wait(HANG_BACKSTOP)                   # one generous hang backstop
    worker.join(timeout=HANG_BACKSTOP)
    assert not worker.is_alive()
    assert errors == []
    assert (published[0] / "imp" / "__init__.py").read_text() == "imp one\n"


def _dead_pid() -> int:
    # A reaped child's pid is gone (and not reused within this test).
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _seed_snapshot(quest_dir: Path, name: str, *, owner: int, body: str) -> Path:
    snapshot = quest_dir / PINNED_SRC_DIRNAME / name
    (snapshot / "imp").mkdir(parents=True)
    (snapshot / "imp" / "__init__.py").write_text(body)
    (snapshot / SOURCE_ROOT_MARKER).write_text(f"{quest_dir / 'elsewhere'}\n")
    (snapshot / OWNER_PID_MARKER).write_text(f"{owner}\n")
    return snapshot


def test_a_pin_leaves_a_live_sibling_snapshot_untouched(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sibling is another daemon's running code. It is stale relative to the source
    tree — which has moved since — and that is exactly why it must not be refreshed or
    removed while its owner lives."""
    src = _fake_source(tmp_path, marker="new")
    monkeypatch.setattr(imp, "__file__", str(src / "imp" / "__init__.py"))
    quest_dir = tmp_path / "quest"
    live = _seed_snapshot(quest_dir, "live-one", owner=os.getpid(), body="imp old\n")

    snapshot = pin_source_snapshot(quest_dir, tmp_path / "control")

    assert snapshot != live
    assert (live / "imp" / "__init__.py").read_text() == "imp old\n"
    assert (live / OWNER_PID_MARKER).read_text() == f"{os.getpid()}\n"
    assert (snapshot / "imp" / "__init__.py").read_text() == "imp new\n"
    assert sorted(p.name for p in (quest_dir / PINNED_SRC_DIRNAME).iterdir()) == sorted(
        ["live-one", snapshot.name])


def test_a_dead_owners_snapshot_is_reaped_but_a_build_directory_is_not(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A published snapshot whose owner pid is gone is disk to reclaim. A temporary
    build directory is indistinguishable from a concurrent cold start's half-built
    snapshot — whatever it contains, dead owner marker included — so it is never
    touched. It sits directly in quest_dir because that is where mkdtemp puts it, so
    this pins the one reaper mis-scoping that can actually occur."""
    src = _fake_source(tmp_path)
    monkeypatch.setattr(imp, "__file__", str(src / "imp" / "__init__.py"))
    quest_dir = tmp_path / "quest"
    dead = _seed_snapshot(quest_dir, "dead-one", owner=_dead_pid(), body="imp gone\n")
    half_built = quest_dir / "pinning-abandoned"
    (half_built / "imp").mkdir(parents=True)
    (half_built / "imp" / "__init__.py").write_text("half\n")
    (half_built / OWNER_PID_MARKER).write_text(f"{_dead_pid()}\n")

    snapshot = pin_source_snapshot(quest_dir, tmp_path / "control")

    assert not dead.exists()
    assert [p.name for p in (quest_dir / PINNED_SRC_DIRNAME).iterdir()] == [snapshot.name]
    assert (half_built / "imp" / "__init__.py").read_text() == "half\n"
    assert (half_built / OWNER_PID_MARKER).exists()


def test_a_stray_file_under_pinned_src_does_not_fail_the_pin(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An already-published snapshot must not be lost to a disk-cleanup hiccup: a
    non-directory entry under pinned-src/ makes lock_is_live raise NotADirectoryError,
    which must be swallowed by the reaper rather than propagated to the caller."""
    src = _fake_source(tmp_path)
    monkeypatch.setattr(imp, "__file__", str(src / "imp" / "__init__.py"))
    quest_dir = tmp_path / "quest"
    (quest_dir / PINNED_SRC_DIRNAME).mkdir(parents=True)
    (quest_dir / PINNED_SRC_DIRNAME / "stray").write_text("noise\n")

    snapshot = pin_source_snapshot(quest_dir, tmp_path / "control")

    assert (snapshot / "imp" / "__init__.py").read_text() == "imp one\n"
    assert (snapshot / OWNER_PID_MARKER).read_text().strip() == str(os.getpid())


def test_two_concurrent_pins_each_publish_their_own_snapshot(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing serializes two cold-starting daemons for one quest, so both must
    succeed with a tree of their own and neither may disturb the other's."""
    src = _fake_source(tmp_path)
    monkeypatch.setattr(imp, "__file__", str(src / "imp" / "__init__.py"))
    quest_dir = tmp_path / "quest"
    control_root = tmp_path / "control"
    published: list[Path] = []
    errors: list[BaseException] = []
    both_ready = threading.Barrier(2)

    def run_pin() -> None:
        try:
            both_ready.wait(timeout=HANG_BACKSTOP)
            published.append(pin_source_snapshot(quest_dir, control_root))
        except BaseException as exc:                     # surfaced by the assert below
            errors.append(exc)

    workers = [threading.Thread(target=run_pin) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=HANG_BACKSTOP)
        assert not worker.is_alive()

    assert errors == []
    assert len(set(published)) == 2
    for snapshot in published:
        assert (snapshot / "imp" / "__init__.py").read_text() == "imp one\n"
        assert (snapshot / OWNER_PID_MARKER).read_text().strip() == str(os.getpid())
    assert sorted(p.name for p in (quest_dir / PINNED_SRC_DIRNAME).iterdir()) == sorted(
        s.name for s in published)
