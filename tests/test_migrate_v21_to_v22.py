# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf

from __future__ import annotations

import json
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from imp.engine import durable
from imp.engine import quest as quest_mod
from imp.engine.quest import LAYOUT_VERSION, Quest, _migrate_v21_to_v22
from imp.engine.reincarnation import handoff_path_for
from imp.engine.resume_state import (
    RESUME_STATE_NAME,
    InvalidResumeState,
    NativeResumeState,
    encode_native_resume_state,
    read_native_resume_state,
)
from imp.providers.codex_rollout import (
    make_codex_resume_state,
    restore_rollout,
)
from tests.conftest import _seed_control

_THREAD_ID = "01993480-c764-7a82-aa98-e2f62b4139e7"
_RESTORE_PATH = "sessions/2026/09/10/rollout-unrelated-name.jsonl"
_ROLLOUT_BYTES = b'{"type":"session_meta","payload":{"id":"legacy"}}\n'
_SLOT = "codex-rollout-a.jsonl"
_LEGACY = "codex_thread.json"
_LEGACY_ROOT = "codex_rollout"


def _seed_v21(control: Path, quest_id: str) -> Path:
    quest = Quest.create_draft(
        control, title="", quest_id=quest_id, base_branch="main")
    meta_path = quest.quest_dir / "quest.json"
    meta = json.loads(meta_path.read_text())
    meta["layout_version"] = 21
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return quest.quest_dir


def _legacy_bytes(
    *,
    thread_id: object = _THREAD_ID,
    restore_path: object = _RESTORE_PATH,
) -> bytes:
    return json.dumps({
        "thread_id": thread_id,
        "rollout_relpath": restore_path,
    }, separators=(",", ":")).encode()


def _seed_legacy(
    quest_dir: Path,
    step: str,
    *,
    phase: str = "phase",
    owner: bytes | None = None,
) -> Path:
    session = quest_dir / "steps" / phase / step
    session.mkdir(parents=True)
    (session / _LEGACY).write_bytes(_legacy_bytes())
    rollout = session / _LEGACY_ROOT
    rollout = rollout.joinpath(*_RESTORE_PATH.split("/"))
    rollout.parent.mkdir(parents=True)
    rollout.write_bytes(_ROLLOUT_BYTES)
    if owner is not None:
        (session / "provider").write_bytes(owner)
    return session


def _manifest_bytes(thread_id: str = _THREAD_ID) -> bytes:
    return encode_native_resume_state(make_codex_resume_state(
        thread_id=thread_id,
        restore_path=_RESTORE_PATH,
        rollout_slot=_SLOT,
    ))


def _seed_current(
    quest_dir: Path,
    step: str,
    *,
    manifest_provider: str,
    owner: bytes | None,
) -> Path:
    session = quest_dir / "steps" / "phase" / step
    session.mkdir(parents=True)
    if manifest_provider == "codex":
        manifest = _manifest_bytes()
        (session / _SLOT).write_bytes(_ROLLOUT_BYTES)
    else:
        manifest = encode_native_resume_state(NativeResumeState(
            provider=manifest_provider,
            state={"session_id": "native"},
        ))
    (session / RESUME_STATE_NAME).write_bytes(manifest)
    if owner is not None:
        (session / "provider").write_bytes(owner)
    return session


def _layout_version(quest_dir: Path) -> int:
    return json.loads((quest_dir / "quest.json").read_text())["layout_version"]


def _snapshot(root: Path) -> tuple[tuple[str, str, bytes | str], ...]:
    rows: list[tuple[str, str, bytes | str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            rows.append((relative, "symlink", path.readlink().as_posix()))
        elif stat.S_ISDIR(mode):
            rows.append((relative, "directory", b""))
        elif stat.S_ISREG(mode):
            rows.append((relative, "file", path.read_bytes()))
        else:
            rows.append((relative, "other", b""))
    return tuple(rows)


def _assert_converted(
    session: Path,
    *,
    owner: bytes = b"codex",
) -> None:
    assert (session / RESUME_STATE_NAME).read_bytes() == _manifest_bytes()
    assert (session / _SLOT).read_bytes() == _ROLLOUT_BYTES
    assert (session / "provider").read_bytes() == owner
    assert not (session / _LEGACY).exists()
    assert read_native_resume_state(session) == make_codex_resume_state(
        thread_id=_THREAD_ID,
        restore_path=_RESTORE_PATH,
        rollout_slot=_SLOT,
    )


def test_load_migrates_active_sessions_and_preserves_native_identity(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    quest_dir = _seed_v21(tmp_path, "mv22-success")
    missing_owner = _seed_legacy(quest_dir, "a")
    existing_owner = _seed_legacy(quest_dir, "b", owner=b"codex\n")
    stash = _seed_legacy(
        quest_dir, "ignored.attempt-1", owner=b"foreign-provider\n")
    stash_before = _snapshot(stash)

    Quest.load(tmp_path, "mv22-success")

    _assert_converted(missing_owner)
    _assert_converted(existing_owner, owner=b"codex\n")
    assert _snapshot(stash) == stash_before
    assert _layout_version(quest_dir) == 22

    codex_home = tmp_path / "restored-codex-home"
    thread_id = restore_rollout(
        codex_home=codex_home,
        transcript_dir=missing_owner,
        resume_state=read_native_resume_state(missing_owner),
    )
    assert (
        thread_id,
        (codex_home.joinpath(*_RESTORE_PATH.split("/"))).read_bytes(),
    ) == (_THREAD_ID, _ROLLOUT_BYTES)

    after_first = _snapshot(quest_dir)
    _migrate_v21_to_v22(quest_dir)
    assert _snapshot(quest_dir) == after_first


def test_load_preserves_proactive_handoff_sibling(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    quest_dir = _seed_v21(tmp_path, "preserved-handoff")
    session = _seed_legacy(quest_dir, "step")
    handoff = handoff_path_for(session)
    handoff_bytes = b"# handoff\nContinue the migration.\n"
    handoff.write_bytes(handoff_bytes)

    Quest.load(tmp_path, "preserved-handoff")

    _assert_converted(session)
    assert handoff.read_bytes() == handoff_bytes
    assert _layout_version(quest_dir) == 22


def test_unknown_phase_file_is_rejected_without_mutation(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    quest_dir = _seed_v21(tmp_path, "unknown-phase-file")
    _seed_legacy(quest_dir, "step")
    (quest_dir / "steps" / "phase" / "unexpected.txt").write_text("unsafe")
    before = _snapshot(quest_dir)

    with pytest.raises(InvalidResumeState):
        _migrate_v21_to_v22(quest_dir)

    assert _snapshot(quest_dir) == before
    assert _layout_version(quest_dir) == 21


def test_the_hop_is_registered_and_the_constant_moved() -> None:
    assert LAYOUT_VERSION == 22
    assert quest_mod._MIGRATIONS[(21, 22)] is _migrate_v21_to_v22


@pytest.mark.parametrize(
    ("manifest_provider", "owner"),
    [
        pytest.param("codex", None, id="codex-missing-provider"),
        pytest.param("codex", b"claude_code", id="codex-mismatched-provider"),
        pytest.param("claude_code", None, id="non-codex-missing-provider"),
        pytest.param("claude_code", b"codex", id="non-codex-mismatched-provider"),
    ],
)
def test_current_manifest_requires_matching_provider_without_mutation(
    tmp_path: Path,
    manifest_provider: str,
    owner: bytes | None,
) -> None:
    _seed_control(tmp_path)
    quest_id = f"current-{manifest_provider}-{owner is None}"
    quest_dir = _seed_v21(tmp_path, quest_id)
    _seed_legacy(quest_dir, "a-good")
    _seed_current(
        quest_dir,
        "z-invalid",
        manifest_provider=manifest_provider,
        owner=owner,
    )
    before = _snapshot(quest_dir)

    with pytest.raises(InvalidResumeState):
        _migrate_v21_to_v22(quest_dir)

    assert _snapshot(quest_dir) == before
    assert _layout_version(quest_dir) == 21


def test_matching_non_codex_current_state_is_untouched_and_advances(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    quest_dir = _seed_v21(tmp_path, "current-unrelated")
    session = _seed_current(
        quest_dir,
        "unrelated",
        manifest_provider="claude_code",
        owner=b"claude_code\n",
    )
    before = _snapshot(session)

    _migrate_v21_to_v22(quest_dir)

    assert _snapshot(session) == before
    assert _layout_version(quest_dir) == 22


def _invalid_utf8(session: Path) -> None:
    (session / _LEGACY).write_bytes(b"\xff")


def _duplicate_key(session: Path) -> None:
    (session / _LEGACY).write_bytes(
        b'{"thread_id":"first","thread_id":"second",'
        b'"rollout_relpath":"sessions/2026/09/10/x.jsonl"}')


def _extra_key(session: Path) -> None:
    (session / _LEGACY).write_bytes(
        b'{"thread_id":"thread","rollout_relpath":'
        b'"sessions/2026/09/10/x.jsonl","extra":true}')


def _empty_thread(session: Path) -> None:
    (session / _LEGACY).write_bytes(_legacy_bytes(thread_id=""))


def _unsafe_path(session: Path) -> None:
    (session / _LEGACY).write_bytes(
        _legacy_bytes(restore_path="sessions/../rollout.jsonl"))


def _missing_rollout(session: Path) -> None:
    session.joinpath(
        _LEGACY_ROOT, *_RESTORE_PATH.split("/")).unlink()


def _foreign_owner(session: Path) -> None:
    (session / "provider").write_bytes(b"claude_code\n")


def _conflicting_slot(session: Path) -> None:
    (session / _SLOT).write_bytes(b"different rollout\n")


def _different_manifest_thread(session: Path) -> None:
    (session / _SLOT).write_bytes(_ROLLOUT_BYTES)
    (session / RESUME_STATE_NAME).write_bytes(
        _manifest_bytes("01993480-c764-7a82-aa98-e2f62b4139e8"))


def _manifest_without_slot(session: Path) -> None:
    (session / RESUME_STATE_NAME).write_bytes(_manifest_bytes())


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(_invalid_utf8, id="invalid-utf8"),
        pytest.param(_duplicate_key, id="duplicate-key"),
        pytest.param(_extra_key, id="extra-key"),
        pytest.param(_empty_thread, id="empty-thread"),
        pytest.param(_unsafe_path, id="unsafe-path"),
        pytest.param(_missing_rollout, id="missing-rollout"),
        pytest.param(_foreign_owner, id="foreign-owner"),
        pytest.param(_conflicting_slot, id="conflicting-slot"),
        pytest.param(_different_manifest_thread, id="different-manifest-thread"),
        pytest.param(_manifest_without_slot, id="manifest-without-slot"),
    ],
)
def test_invalid_declared_state_is_rejected_before_any_mutation(
    tmp_path: Path,
    corrupt: Callable[[Path], None],
) -> None:
    _seed_control(tmp_path)
    quest_dir = _seed_v21(tmp_path, f"reject-{corrupt.__name__}")
    _seed_legacy(quest_dir, "a-good")
    invalid = _seed_legacy(quest_dir, "z-invalid")
    corrupt(invalid)
    before = _snapshot(quest_dir)

    with pytest.raises(InvalidResumeState):
        _migrate_v21_to_v22(quest_dir)

    assert _snapshot(quest_dir) == before
    assert _layout_version(quest_dir) == 21


@pytest.mark.parametrize("linked_component", ["phase", "session"])
def test_symlinked_active_layout_is_rejected_without_touching_external_state(
    tmp_path: Path,
    linked_component: str,
) -> None:
    _seed_control(tmp_path)
    quest_dir = _seed_v21(tmp_path, f"symlink-{linked_component}")
    steps = quest_dir / "steps"
    external = tmp_path / f"external-{linked_component}"

    if linked_component == "phase":
        _seed_legacy(external, "step", phase="outside")
        (steps / "phase").symlink_to(
            external / "steps" / "outside", target_is_directory=True)
    else:
        phase = steps / "phase"
        phase.mkdir()
        external_session = _seed_legacy(external, "step", phase="outside")
        (phase / "step").symlink_to(external_session, target_is_directory=True)

    quest_before = _snapshot(quest_dir)
    external_before = _snapshot(external)

    with pytest.raises(InvalidResumeState):
        _migrate_v21_to_v22(quest_dir)

    assert _snapshot(quest_dir) == quest_before
    assert _snapshot(external) == external_before
    assert _layout_version(quest_dir) == 21


def test_symlinked_legacy_rollout_component_cannot_escape_its_root(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    quest_dir = _seed_v21(tmp_path, "rollout-symlink")
    session = quest_dir / "steps" / "phase" / "step"
    session.mkdir(parents=True)
    (session / _LEGACY).write_bytes(_legacy_bytes())
    legacy_root = session / _LEGACY_ROOT
    legacy_root.mkdir()
    external = tmp_path / "external-rollout"
    source = external.joinpath(*_RESTORE_PATH.split("/"))
    source.parent.mkdir(parents=True)
    source.write_bytes(_ROLLOUT_BYTES)
    (legacy_root / "sessions").symlink_to(
        external / "sessions", target_is_directory=True)
    quest_before = _snapshot(quest_dir)
    external_before = _snapshot(external)

    with pytest.raises(InvalidResumeState):
        _migrate_v21_to_v22(quest_dir)

    assert _snapshot(quest_dir) == quest_before
    assert _snapshot(external) == external_before
    assert (session / _LEGACY).read_bytes() == _legacy_bytes()
    assert _layout_version(quest_dir) == 21


_CRASH_BOUNDARIES = [
    "slot_write",
    "slot_file_fsync",
    "slot_replace",
    "slot_dir_fsync",
    "manifest_write",
    "manifest_file_fsync",
    "manifest_replace",
    "manifest_dir_fsync",
    "provider_write",
    "provider_file_fsync",
    "provider_replace",
    "provider_dir_fsync",
    "legacy_unlink",
    "legacy_dir_fsync",
    "marker_write",
    "marker_file_fsync",
    "marker_replace",
    "marker_dir_fsync",
]


def _publication_kind(path: Path) -> str | None:
    name = path.name
    if name == _SLOT or name.startswith(".codex-rollout-a-"):
        return "slot"
    if name == RESUME_STATE_NAME or name.startswith(".resume-state-"):
        return "manifest"
    if name == "provider" or name.startswith(".provider-"):
        return "provider"
    if name == "quest.json" or name.startswith(".quest-"):
        return "marker"
    return None


def _install_crash_after(
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    real_durable_replace = durable.durable_replace
    real_fsync_file = durable._fsync_file
    real_replace = durable.os.replace
    real_fsync_directory = durable.fsync_directory
    real_unlink = Path.unlink
    completed: str | None = None

    def crash_writer(
        path: Path,
        writer: Callable[[BinaryIO], object],
    ) -> None:
        kind = _publication_kind(path)

        def wrapped_writer(stream: BinaryIO) -> object:
            result = writer(stream)
            if failure_point == f"{kind}_write":
                raise OSError(f"crash after {failure_point}")
            return result

        real_durable_replace(path, wrapped_writer)

    def crash_file_fsync(stream: BinaryIO, path: Path) -> None:
        real_fsync_file(stream, path)
        kind = _publication_kind(path)
        if failure_point == f"{kind}_file_fsync":
            raise OSError(f"crash after {failure_point}")

    def crash_replace(source: Path, destination: Path) -> None:
        nonlocal completed
        real_replace(source, destination)
        completed = _publication_kind(Path(destination))
        if failure_point == f"{completed}_replace":
            raise OSError(f"crash after {failure_point}")

    def crash_directory_fsync(path: Path) -> None:
        real_fsync_directory(path)
        if failure_point == f"{completed}_dir_fsync":
            raise OSError(f"crash after {failure_point}")

    def crash_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal completed
        real_unlink(path, *args, **kwargs)
        if path.name == _LEGACY:
            completed = "legacy"
            if failure_point == "legacy_unlink":
                raise OSError(f"crash after {failure_point}")

    monkeypatch.setattr(durable, "durable_replace", crash_writer)
    monkeypatch.setattr(durable, "_fsync_file", crash_file_fsync)
    monkeypatch.setattr(durable.os, "replace", crash_replace)
    monkeypatch.setattr(durable, "fsync_directory", crash_directory_fsync)
    monkeypatch.setattr(Path, "unlink", crash_unlink)


@pytest.mark.parametrize("failure_point", _CRASH_BOUNDARIES)
def test_every_completed_operation_is_rerunnable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    _seed_control(tmp_path)
    quest_id = f"crash-{failure_point.replace('_', '-')}"
    quest_dir = _seed_v21(tmp_path, quest_id)
    session = _seed_legacy(quest_dir, "step")

    with monkeypatch.context() as crash:
        _install_crash_after(crash, failure_point)
        with pytest.raises(OSError, match=f"crash after {failure_point}"):
            Quest.load(tmp_path, quest_id)

    Quest.load(tmp_path, quest_id)

    _assert_converted(session)
    assert _layout_version(quest_dir) == 22


@pytest.mark.parametrize("first_failure", ["slot_replace", "legacy_unlink"])
def test_recovery_fsync_barrier_precedes_every_new_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_failure: str,
) -> None:
    _seed_control(tmp_path)
    quest_id = f"barrier-{first_failure.replace('_', '-')}"
    quest_dir = _seed_v21(tmp_path, quest_id)
    session = _seed_legacy(quest_dir, "step")

    with monkeypatch.context() as first_crash:
        _install_crash_after(first_crash, first_failure)
        with pytest.raises(OSError, match=f"crash after {first_failure}"):
            _migrate_v21_to_v22(quest_dir)

    assert (session / _SLOT).read_bytes() == _ROLLOUT_BYTES
    if first_failure == "slot_replace":
        assert not (session / RESUME_STATE_NAME).exists()
        assert not (session / "provider").exists()
        assert (session / _LEGACY).exists()
    else:
        assert (session / RESUME_STATE_NAME).read_bytes() == _manifest_bytes()
        assert (session / "provider").read_bytes() == b"codex"
        assert not (session / _LEGACY).exists()
    assert _layout_version(quest_dir) == 21

    attempted_publications: list[Path] = []
    real_durable_replace = durable.durable_replace
    real_fsync_directory = durable.fsync_directory

    def observe_publication(
        path: Path,
        writer: Callable[[BinaryIO], object],
    ) -> None:
        attempted_publications.append(path)
        real_durable_replace(path, writer)

    def fail_recovery_fsync(path: Path) -> None:
        if path == session:
            raise OSError("second crash before recovery fsync")
        real_fsync_directory(path)

    with monkeypatch.context() as second_crash:
        second_crash.setattr(durable, "durable_replace", observe_publication)
        second_crash.setattr(durable, "fsync_directory", fail_recovery_fsync)
        with pytest.raises(
            OSError, match="second crash before recovery fsync",
        ):
            _migrate_v21_to_v22(quest_dir)

    assert attempted_publications == []
    assert _layout_version(quest_dir) == 21

    Quest.load(tmp_path, quest_id)
    _assert_converted(session)
    assert _layout_version(quest_dir) == 22
