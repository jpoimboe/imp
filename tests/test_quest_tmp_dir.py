from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from imp.engine.abandon import archive_conversation, discard_conversation, tear_down_quest
from imp.engine.env import ensure_quest_tmp_dir
from imp.engine.executor import _archive_and_reap
from imp.engine.quest import Quest
from imp.remote.session.attach import spawn_session_daemon
from tests.conftest import _seed_control


def test_tmp_dir_is_per_quest_subdir_of_imp_uid(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="T", quest_id="qtmp1", base_branch="main")
    assert ch.tmp_dir.name == "qtmp1"
    assert ch.tmp_dir.parent.name == f"imp-{os.getuid()}"
    assert ch.tmp_dir.parent.parent == Path(tempfile.gettempdir())


def test_quest_tmp_root_is_isolated_per_test() -> None:
    # The autouse fixture redirects the tmp root (Quest.tmp_dir) to a short unique
    # per-test dir. The session socket now lives as a sibling under that same tmp
    # root (<tmpdir>/imp-<uid>/<id>.sock).
    tmpdir = tempfile.gettempdir()
    assert "imp-t-" in tmpdir
    assert Path(tmpdir).is_dir()


def test_var_tmp_root_is_isolated_per_test() -> None:
    # Fixture redirects /var/tmp to a unique real-disk per-test dir (overlayfs needs real disk).
    from imp.engine.env import quest_var_tmp_root

    root = quest_var_tmp_root()
    assert root != Path("/var/tmp")
    assert root.parent == Path("/var/tmp")
    assert root.name.startswith("imp-t-")
    assert root.is_dir()


def test_ensure_quest_tmp_dir_creates_0o700_parent_and_leaf(tmp_path: Path) -> None:
    # The autouse _isolate_quest_tmp_root fixture already points the tmp root at a
    # short unique per-test dir, so the created tree is hermetic and auto-cleaned.
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="T", quest_id="ensq1", base_branch="main")

    old_umask = os.umask(0)          # a permissive umask would leave the parent 0o777
    try:
        ensure_quest_tmp_dir(ch.tmp_dir)
    finally:
        os.umask(old_umask)

    leaf = ch.tmp_dir
    assert leaf.is_dir()
    assert leaf.parent.is_dir()
    assert leaf.stat().st_mode & 0o777 == 0o700
    assert leaf.parent.stat().st_mode & 0o777 == 0o700

    # Pre-existing dirs at the wrong mode must be corrected, and a repeat call
    # (resume-after-reap / live-daemon-already-holds-dir) must stay idempotent.
    leaf.chmod(0o755)
    leaf.parent.chmod(0o755)
    ensure_quest_tmp_dir(ch.tmp_dir)
    assert leaf.is_dir()
    assert leaf.stat().st_mode & 0o777 == 0o700
    assert leaf.parent.stat().st_mode & 0o777 == 0o700


def test_quest_tmp_dir_equals_resolve_policy_bind_source(tmp_path: Path) -> None:
    # The executor wipes Quest.tmp_dir while the sandbox binds resolve_policy's value;
    # drift would silently leave the bound scratch dir un-wiped.
    from imp.engine.sandbox import SandboxConfig, resolve_policy

    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="T", quest_id="qtmp2", base_branch="main")
    pol = resolve_policy(
        worktree=ch.project_root, control_root=ch.control_root,
        quest_id=ch.id, cfg=SandboxConfig(uid=99999),
        engine_env_result={}, environ={}, available=True, quest_dir=ch.quest_dir)
    assert pol.quest_tmp_dir == str(ch.tmp_dir)
    assert (str(ch.tmp_dir), "rw") in pol.bind_plan()
    assert ("TMPDIR", str(ch.tmp_dir)) in pol.child_env
    assert "imp-99999" not in pol.quest_tmp_dir


def test_quest_scratch_root_prefers_imp_quest_tmp_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.engine.env import quest_scratch_root

    monkeypatch.setenv("IMP_QUEST_TMP", "/some/quest/tmp")
    assert quest_scratch_root() == Path("/some/quest/tmp")
    monkeypatch.delenv("IMP_QUEST_TMP", raising=False)
    assert quest_scratch_root() == Path(tempfile.gettempdir())


def test_quest_var_tmp_root_prefers_imp_quest_var_tmp_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from imp.engine.env import quest_var_tmp_root

    monkeypatch.setenv("IMP_QUEST_VAR_TMP", "/some/var/tmp")
    assert quest_var_tmp_root() == Path("/some/var/tmp")
    monkeypatch.delenv("IMP_QUEST_VAR_TMP", raising=False)
    assert quest_var_tmp_root() == Path("/var/tmp")


def test_spawn_session_daemon_exports_quest_tmp_without_poisoning_tmpdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon is spawned with IMP_QUEST_TMP=<quest.tmp_dir> so codex/claude
    homes land under the per-quest dir. The daemon env must NOT set TMPDIR: the
    daemon derives Quest.tmp_dir and the socket from its own paths; only the
    in-sandbox child_env carries the per-quest TMPDIR."""
    from imp.remote.session import attach as attach_mod

    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="T", quest_id="tmpdirq", base_branch="main")

    captured: dict[str, dict[str, str]] = {}

    def fake_popen(argv, **kwargs):  # type: ignore[no-untyped-def]
        captured["env"] = kwargs["env"]
        return object()

    monkeypatch.setattr(attach_mod.subprocess, "Popen", fake_popen)

    spawn_session_daemon(ch)

    assert captured["env"]["IMP_QUEST_TMP"] == str(ch.tmp_dir)
    # The daemon env must NOT set TMPDIR: it derives Quest.tmp_dir and the socket
    # from its own paths; only the in-sandbox child_env carries the per-quest TMPDIR.
    assert captured["env"].get("TMPDIR") != str(ch.tmp_dir)


def test_var_tmp_dir_is_per_quest_subdir_of_var_tmp_imp_uid(tmp_path: Path) -> None:
    from imp.engine.env import quest_var_tmp_root

    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="T", quest_id="qvt1", base_branch="main")
    assert ch.var_tmp_dir == quest_var_tmp_root() / f"imp-{os.getuid()}" / "qvt1"


def test_var_tmp_dir_derives_from_quest_var_tmp_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="T", quest_id="qvt3", base_branch="main")
    monkeypatch.setenv("IMP_QUEST_VAR_TMP", "/custom/vroot")
    assert ch.var_tmp_dir == Path("/custom/vroot") / f"imp-{os.getuid()}" / "qvt3"


def test_var_tmp_dir_equals_resolve_policy_source_even_with_injected_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC4: quest.var_tmp_dir equals resolve_policy's bound source (both use os.getuid()).
    import imp.engine.sandbox as sandbox_mod
    from imp.engine.env import quest_var_tmp_root
    from imp.engine.sandbox import SandboxConfig, resolve_policy

    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="T", quest_id="qvt2", base_branch="main")
    root = quest_var_tmp_root()
    real_access = os.access
    monkeypatch.setattr(
        sandbox_mod.os, "access",
        lambda p, m: True if str(p) == str(root) else real_access(p, m))
    pol = resolve_policy(
        worktree=ch.project_root, control_root=ch.control_root,
        quest_id=ch.id, cfg=SandboxConfig(uid=99999),
        engine_env_result={}, environ={}, available=True, quest_dir=ch.quest_dir)
    assert pol.quest_var_tmp_dir == str(ch.var_tmp_dir)
    assert "imp-99999" not in pol.quest_var_tmp_dir


def test_resolve_policy_var_tmp_derives_from_quest_var_tmp_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.engine.sandbox as sandbox_mod
    from imp.engine.sandbox import SandboxConfig, resolve_policy

    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="T", quest_id="qvt4", base_branch="main")
    monkeypatch.setenv("IMP_QUEST_VAR_TMP", "/custom/vroot")
    real_access = os.access
    monkeypatch.setattr(
        sandbox_mod.os, "access",
        lambda p, m: True if str(p) == "/custom/vroot" else real_access(p, m))
    pol = resolve_policy(
        worktree=ch.project_root, control_root=ch.control_root,
        quest_id=ch.id, cfg=SandboxConfig(uid=99999),
        engine_env_result={}, environ={}, available=True, quest_dir=ch.quest_dir)
    assert pol.quest_var_tmp_dir == str(
        Path("/custom/vroot") / f"imp-{os.getuid()}" / "qvt4")
    assert pol.quest_var_tmp_dir == str(ch.var_tmp_dir)
    assert "imp-99999" not in pol.quest_var_tmp_dir


def test_wipe_step_scratch_empties_in_place_without_changing_the_inode(
    tmp_path: Path,
) -> None:
    # The sandbox binds this directory, and a bind resolves the inode it saw at
    # namespace creation — recreating the dir would drop the bind to the ro floor.
    from imp.engine.executor import _wipe_step_scratch

    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="T", quest_id="wipe1", base_branch="main")
    ensure_quest_tmp_dir(ch.tmp_dir)
    before_ino = ch.tmp_dir.stat().st_ino

    real_dir = tmp_path / "outside"
    real_dir.mkdir()
    (real_dir / "sentinel.txt").write_text("keep")
    (ch.tmp_dir / "loose.txt").write_text("x")
    (ch.tmp_dir / "nest" / "deeper").mkdir(parents=True)
    (ch.tmp_dir / "nest" / "deeper" / "g.txt").write_text("y")
    (ch.tmp_dir / "dangling").symlink_to(ch.tmp_dir / "gone")
    (ch.tmp_dir / "symdir").symlink_to(real_dir)
    slot = ch.tmp_dir / "explorer-1"          # a per-agent scratch slot
    ensure_quest_tmp_dir(slot)
    (slot / "probe.txt").write_text("z")
    ch.tmp_dir.chmod(0o755)

    _wipe_step_scratch(ch)

    assert ch.tmp_dir.stat().st_ino == before_ino
    assert list(ch.tmp_dir.iterdir()) == []
    assert not slot.exists()                  # the slot went with everything else
    assert ch.tmp_dir.stat().st_mode & 0o777 == 0o700
    assert (real_dir / "sentinel.txt").exists()  # the dir symlink was not followed


def test_wipe_step_scratch_creates_the_dir_when_it_is_missing(tmp_path: Path) -> None:
    from imp.engine.executor import _wipe_step_scratch

    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="T", quest_id="wipe2", base_branch="main")
    assert not ch.tmp_dir.exists()

    _wipe_step_scratch(ch)

    assert ch.tmp_dir.is_dir()
    assert ch.tmp_dir.stat().st_mode & 0o777 == 0o700


def test_ensure_quest_tmp_dir_locks_down_every_level_it_creates(tmp_path: Path) -> None:
    # A slot leaf sits BELOW the quest dir, so mkdir(parents=True, mode=) creates an
    # intermediate the leaf+parent chmod never reaches — left at the umask default.
    root = tmp_path / f"imp-{os.getuid()}"
    root.mkdir()
    leaf = root / "qid" / "explorer-1" / "spiker-2"

    old_umask = os.umask(0)          # a permissive umask would leave a level 0o777
    try:
        ensure_quest_tmp_dir(leaf)
    finally:
        os.umask(old_umask)

    for d in (root / "qid", leaf.parent, leaf):
        assert d.is_dir(), d
        assert d.stat().st_mode & 0o777 == 0o700, d


def test_ensure_quest_tmp_dir_leaves_pre_existing_ancestors_above_the_parent_alone(
    tmp_path: Path,
) -> None:
    # imp's to relock: the leaf, its parent, and levels this call created. An ancestor
    # that was already there is not, so its mode must survive untouched.
    root = tmp_path / f"imp-{os.getuid()}"
    root.mkdir()
    root.chmod(0o755)
    quest_dir = root / "qid"
    quest_dir.mkdir()
    quest_dir.chmod(0o700)
    leaf = quest_dir / "explorer-1"

    ensure_quest_tmp_dir(leaf)

    assert root.stat().st_mode & 0o777 == 0o755
    assert quest_dir.stat().st_mode & 0o777 == 0o700
    assert leaf.stat().st_mode & 0o777 == 0o700


@pytest.fixture
def plant_read_only() -> Iterator[Callable[[Path], Path]]:
    """Plant a credential-copy-shaped subtree: a 0o500 dir holding a 0o400 file."""
    planted: list[Path] = []

    def plant(parent: Path) -> Path:
        ro = parent / "provider-auth"
        ro.mkdir()
        (ro / "auth.json").write_text("{}")
        (ro / "auth.json").chmod(0o400)
        ro.chmod(0o500)
        planted.append(ro)
        return ro

    yield plant
    for ro in planted:  # on failure, let the per-test temp-root's plain rmtree reap it
        if ro.exists():
            ro.chmod(0o700)


def _reap_merged(quest: Quest) -> None:
    _archive_and_reap(quest, quest.event_log())


@pytest.mark.parametrize("teardown", [
    pytest.param(_reap_merged, id="archive_and_reap"),
    pytest.param(tear_down_quest, id="tear_down_quest"),
    pytest.param(archive_conversation, id="archive_conversation"),
    pytest.param(discard_conversation, id="discard_conversation"),
])
def test_quest_end_removes_a_read_only_tmp_subtree(
    tmp_path: Path, plant_read_only: Callable[[Path], Path],
    teardown: Callable[[Quest], None], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.engine.sandbox as sandbox_mod
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="T", quest_id="roend", base_branch="main")
    ensure_quest_tmp_dir(ch.tmp_dir)
    ro = plant_read_only(ch.tmp_dir)
    real_chmod = os.chmod
    chmodded: list[str] = []
    def _spy_chmod(p, mode, *a, **k):
        chmodded.append(str(p))
        return real_chmod(p, mode, *a, **k)
    monkeypatch.setattr(sandbox_mod.os, "chmod", _spy_chmod)

    teardown(ch)

    assert not ch.tmp_dir.exists()
    assert str(ro) in chmodded  # proves force_rmtree's chmod walk ran
