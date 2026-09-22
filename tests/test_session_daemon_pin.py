"""daemon.main pins its source and re-execs out of it exactly once; a pin that cannot
be made is fatal before any socket is bound."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import filelock
import pytest

import imp
import imp.remote.session.pin as pin_mod
from imp.paths import project_state_dir
from imp.remote.session import daemon
from imp.remote.session.pin import OWNER_PID_MARKER, PINNED_SRC_DIRNAME


class _Execd(Exception):
    """Raised by the fake execv so the test regains control exactly where the real one
    would have replaced the process image."""


def _refuse_exec(path: str, argv: list[str]) -> None:
    raise _Execd(path)


def _small_source(root: Path) -> Path:
    """The two packages pyproject ships, small enough to copy in a test."""
    src = root / "src"
    for pkg in ("imp", "impty"):
        (src / pkg).mkdir(parents=True)
        (src / pkg / "__init__.py").write_text(f"{pkg}\n")
    return src


def test_an_unpinned_daemon_pins_and_reexecs_from_the_snapshot(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The exec is what makes the snapshot authoritative for this process — including
    warm_module_graph, which then warms from snapshot bytes. The snapshot travels in a
    -c bootstrap, never on PYTHONPATH, which every child would inherit."""
    src = _small_source(tmp_path)
    monkeypatch.setattr(imp, "__file__", str(src / "imp" / "__init__.py"))
    captured: dict[str, object] = {}

    def fake_execv(path: str, argv: list[str]) -> None:
        captured["path"] = path
        captured["argv"] = argv
        captured["pythonpath"] = os.environ.get("PYTHONPATH", "")
        raise _Execd(path)

    monkeypatch.setattr(daemon.os, "execv", fake_execv)

    with pytest.raises(_Execd):
        daemon.main([
            "--control-root", str(tmp_path), "--quest-id", "feat", "--resume"])

    quest_dir = project_state_dir(tmp_path) / "quests" / "feat"
    snapshots = list((quest_dir / PINNED_SRC_DIRNAME).iterdir())
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert (snapshot / "imp" / "__init__.py").read_text() == "imp\n"
    assert (snapshot / OWNER_PID_MARKER).read_text().strip() == str(os.getpid())
    argv = captured["argv"]
    assert captured["path"] == sys.executable
    assert argv[:2] == [sys.executable, "-c"]
    assert argv[3:] == [
        "--pinned", "--control-root", str(tmp_path), "--quest-id", "feat", "--resume"]
    bootstrap = str(argv[2])
    assert repr(str(snapshot)) in bootstrap
    assert "from imp.remote.session.daemon import main" in bootstrap
    assert str(snapshot) not in str(captured["pythonpath"])


def test_a_pinned_daemon_neither_copies_nor_execs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-entering main inside the snapshot must fall straight through to startup, or
    the daemon would pin and exec forever."""
    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("a pinned daemon must not pin or exec again")

    quest_dir = project_state_dir(tmp_path) / "quests" / "feat"
    monkeypatch.setattr(daemon, "pin_source_snapshot", boom)
    monkeypatch.setattr(daemon.os, "execv", boom)
    monkeypatch.setattr(daemon, "warm_module_graph", lambda: None)
    monkeypatch.setattr(daemon, "install_stack_dumper", lambda p: None)
    monkeypatch.setattr(
        daemon.Quest, "load",
        classmethod(lambda cls, cr, qid: SimpleNamespace(quest_dir=quest_dir)))
    monkeypatch.setattr(daemon.anyio, "run", lambda *a, **k: object())

    rc = daemon.main(["--pinned", "--control-root", str(tmp_path), "--quest-id", "feat"])

    assert rc == 0
    assert not (quest_dir / PINNED_SRC_DIRNAME).exists()


def test_the_pin_leaves_the_quest_lock_file_untouched(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """daemon.py's crash detection reads <quest_dir>/lock before ProjectLock.acquire();
    a pin that created or rewrote it would make a cold start look like a crash."""
    src = _small_source(tmp_path)
    monkeypatch.setattr(imp, "__file__", str(src / "imp" / "__init__.py"))
    monkeypatch.setattr(daemon.os, "execv", _refuse_exec)
    quest_dir = project_state_dir(tmp_path) / "quests" / "feat"
    quest_dir.mkdir(parents=True, exist_ok=True)
    lock = quest_dir / "lock"
    lock.write_text("4242")
    before = lock.stat().st_mtime_ns

    with pytest.raises(_Execd):
        daemon.main(["--control-root", str(tmp_path), "--quest-id", "feat"])

    assert lock.read_text() == "4242"
    assert lock.stat().st_mtime_ns == before


def test_a_missing_impty_fails_startup_with_the_pin_message(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """An unpinned run is the defect this quest removes, so a pin that cannot be made
    exits before the socket binds rather than serving unpinned."""
    src = _small_source(tmp_path)
    shutil.rmtree(src / "impty")
    monkeypatch.setattr(imp, "__file__", str(src / "imp" / "__init__.py"))
    monkeypatch.setattr(daemon.os, "execv", _refuse_exec)
    served: list[object] = []
    monkeypatch.setattr(daemon.anyio, "run", lambda *a, **k: served.append(a))

    rc = daemon.main(["--control-root", str(tmp_path), "--quest-id", "feat"])

    assert rc == 2
    assert capsys.readouterr().err.splitlines() == [
        "imp: could not pin this run's imp source: "
        f"no impty package beside the running imp at {(src / 'imp').resolve()}"]
    quest_dir = project_state_dir(tmp_path) / "quests" / "feat"
    assert served == []                       # nothing served: no socket, no ProjectLock
    assert not (quest_dir / "lock").exists()
    assert not (quest_dir / PINNED_SRC_DIRNAME).exists()


def test_a_repo_lock_timeout_is_the_same_fatal_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """RepoGitLock raises rather than blocking forever; a lock imp cannot take is a
    snapshot imp cannot trust."""
    src = _small_source(tmp_path)
    monkeypatch.setattr(imp, "__file__", str(src / "imp" / "__init__.py"))
    lock_path = tmp_path / ".git" / "imp" / "repo.lock"

    class _TimingOutLock:
        def __init__(self, control_root: Path) -> None:
            self.path = lock_path

        def acquire(self, timeout: float = 60.0) -> None:
            raise filelock.Timeout(str(lock_path))

    monkeypatch.setattr(pin_mod, "RepoGitLock", _TimingOutLock)
    monkeypatch.setattr(daemon.os, "execv", _refuse_exec)
    served: list[object] = []
    monkeypatch.setattr(daemon.anyio, "run", lambda *a, **k: served.append(a))

    rc = daemon.main(["--control-root", str(tmp_path), "--quest-id", "feat"])

    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("imp: could not pin this run's imp source: ")
    assert str(lock_path) in err
    assert served == []
    quest_dir = project_state_dir(tmp_path) / "quests" / "feat"
    assert not (quest_dir / PINNED_SRC_DIRNAME).exists()
