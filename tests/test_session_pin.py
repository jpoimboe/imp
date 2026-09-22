from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

import imp
import imp.remote.session.pin as pin_mod
from imp.engine.shim import SOURCE_ROOT_MARKER
from imp.remote.session.pin import (
    OWNER_PID_MARKER,
    PINNED_SRC_DIRNAME,
    pin_source_snapshot,
)


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
