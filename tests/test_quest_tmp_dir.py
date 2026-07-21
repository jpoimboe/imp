from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from imp.engine.quest import Quest
from imp.remote.session.attach import ensure_quest_tmp_dir, spawn_session_daemon
from imp.remote.session.discover import session_socket_path
from tests.conftest import _seed_control


def test_tmp_dir_is_per_quest_subdir_of_imp_uid(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="T", quest_id="qtmp1", base_branch="main")
    assert ch.tmp_dir.name == "qtmp1"
    assert ch.tmp_dir.parent.name == f"imp-{os.getuid()}"
    assert ch.tmp_dir.parent.parent == Path(tempfile.gettempdir())


def test_quest_tmp_root_is_isolated_per_test() -> None:
    # The autouse fixture redirects the tmp root to a short unique per-test dir,
    # so session sockets never share the raw system-temp /tmp/imp-<uid> root.
    tmpdir = tempfile.gettempdir()
    assert "imp-t-" in tmpdir
    assert Path(tmpdir).is_dir()
    sock = session_socket_path(Path("/any/control"), "some-id")
    assert str(sock).startswith(tmpdir)


def test_ensure_quest_tmp_dir_creates_0o700_parent_and_leaf(tmp_path: Path) -> None:
    # The autouse _isolate_quest_tmp_root fixture already points the tmp root at a
    # short unique per-test dir, so the created tree is hermetic and auto-cleaned.
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="T", quest_id="ensq1", base_branch="main")

    ensure_quest_tmp_dir(ch)

    leaf = ch.tmp_dir
    assert leaf.is_dir()
    assert leaf.parent.is_dir()
    assert leaf.stat().st_mode & 0o777 == 0o700
    assert leaf.parent.stat().st_mode & 0o777 == 0o700

    # Pre-existing dirs at the wrong mode must be corrected, and a repeat call
    # (resume-after-reap / live-daemon-already-holds-dir) must stay idempotent.
    leaf.chmod(0o755)
    leaf.parent.chmod(0o755)
    ensure_quest_tmp_dir(ch)
    assert leaf.is_dir()
    assert leaf.stat().st_mode & 0o777 == 0o700
    assert leaf.parent.stat().st_mode & 0o777 == 0o700


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
    homes land under the per-quest dir — but TMPDIR is NOT repointed at the
    per-quest leaf: the daemon's tempfile.gettempdir() also derives
    session_socket_path and Quest.tmp_dir, so poisoning it would double-nest the
    socket and break attach."""
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
    # Regression guard for the Critical: TMPDIR must NOT be set to the per-quest
    # leaf (that is exactly what double-nests session_socket_path in the daemon).
    assert captured["env"].get("TMPDIR") != str(ch.tmp_dir)


def test_var_tmp_dir_is_per_quest_subdir_of_var_tmp_imp_uid(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="T", quest_id="qvt1", base_branch="main")
    assert ch.var_tmp_dir == Path("/var/tmp") / f"imp-{os.getuid()}" / "qvt1"


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
    from imp.engine.sandbox import SandboxConfig, resolve_policy

    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="T", quest_id="qvt2", base_branch="main")
    real_access = os.access
    monkeypatch.setattr(
        sandbox_mod.os, "access",
        lambda p, m: True if str(p) == "/var/tmp" else real_access(p, m))
    pol = resolve_policy(
        worktree=ch.project_root, control_root=ch.control_root,
        quest_id=ch.id, cfg=SandboxConfig(uid=99999),
        engine_env_result={}, environ={}, available=True, quest_dir=ch.quest_dir)
    assert pol.quest_var_tmp_dir == str(ch.var_tmp_dir)
    assert "imp-99999" not in pol.quest_var_tmp_dir
