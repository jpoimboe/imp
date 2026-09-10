# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from imp.engine import durable
from imp.engine.resume_state import (
    RESUME_STATE_NAME,
    InvalidResumeState,
    NativeResumeState,
    encode_native_resume_state,
    read_native_resume_state,
)
from imp.providers.codex_rollout import (
    CODEX_ROLLOUT_SLOTS,
    make_codex_resume_state,
    parse_codex_resume_state,
)

_LEGACY_METADATA = "codex_thread.json"
_LEGACY_ROLLOUT_ROOT = "codex_rollout"


@dataclass(frozen=True, slots=True)
class _Conversion:
    session_dir: Path
    rollout_bytes: bytes
    manifest_bytes: bytes
    write_slot: bool
    write_manifest: bool
    write_owner: bool
    legacy_metadata: Path


def _entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _regular_bytes(path: Path) -> bytes:
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise InvalidResumeState()
        return path.read_bytes()
    except OSError:
        raise InvalidResumeState() from None


def _legacy_rollout_bytes(session_dir: Path, restore_path: str) -> bytes:
    legacy_root = session_dir / _LEGACY_ROLLOUT_ROOT
    source = legacy_root.joinpath(*restore_path.split("/"))
    try:
        if not stat.S_ISDIR(legacy_root.lstat().st_mode):
            raise InvalidResumeState()
        resolved_session = session_dir.resolve(strict=True)
        resolved_root = legacy_root.resolve(strict=True)
        if not resolved_root.is_relative_to(resolved_session):
            raise InvalidResumeState()
        if not stat.S_ISREG(source.lstat().st_mode):
            raise InvalidResumeState()
        if not source.resolve(strict=True).is_relative_to(resolved_root):
            raise InvalidResumeState()
        return source.read_bytes()
    except OSError:
        raise InvalidResumeState() from None


def _object_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidResumeState()
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise InvalidResumeState()


def parse_legacy_codex_resume_state(raw: bytes) -> NativeResumeState:
    try:
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InvalidResumeState() from None
    if not isinstance(data, dict) or set(data) != {
        "thread_id", "rollout_relpath",
    }:
        raise InvalidResumeState()
    return make_codex_resume_state(
        thread_id=data["thread_id"],
        restore_path=data["rollout_relpath"],
        rollout_slot=CODEX_ROLLOUT_SLOTS[0],
    )


def _validate_current_state(session_dir: Path) -> bool:
    current = read_native_resume_state(session_dir)
    present_slots = [
        session_dir / name
        for name in CODEX_ROLLOUT_SLOTS
        if _entry_exists(session_dir / name)
    ]
    if current is None:
        if present_slots:
            raise InvalidResumeState()
        return False

    owner_bytes = _regular_bytes(session_dir / "provider")
    try:
        owner = owner_bytes.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise InvalidResumeState() from None
    if owner != current.provider:
        raise InvalidResumeState()

    if current.provider != "codex":
        if present_slots:
            raise InvalidResumeState()
        return False

    selected = parse_codex_resume_state(current)
    _regular_bytes(session_dir / selected.rollout_slot)
    for slot in present_slots:
        _regular_bytes(slot)
    return True


def _preflight_legacy(session_dir: Path) -> _Conversion:
    legacy_metadata = session_dir / _LEGACY_METADATA
    legacy_state = parse_legacy_codex_resume_state(
        _regular_bytes(legacy_metadata))
    selected = parse_codex_resume_state(legacy_state)
    rollout_bytes = _legacy_rollout_bytes(
        session_dir, selected.restore_path)

    owner_path = session_dir / "provider"
    write_owner = not _entry_exists(owner_path)
    if not write_owner:
        owner_bytes = _regular_bytes(owner_path)
        try:
            owner = owner_bytes.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise InvalidResumeState() from None
        if owner != "codex":
            raise InvalidResumeState()

    selected_slot = session_dir / selected.rollout_slot
    write_slot = not _entry_exists(selected_slot)
    if not write_slot and _regular_bytes(selected_slot) != rollout_bytes:
        raise InvalidResumeState()
    if _entry_exists(session_dir / CODEX_ROLLOUT_SLOTS[1]):
        raise InvalidResumeState()

    manifest_path = session_dir / RESUME_STATE_NAME
    write_manifest = not _entry_exists(manifest_path)
    if not write_manifest:
        if write_slot:
            raise InvalidResumeState()
        current = read_native_resume_state(session_dir)
        if current is None or parse_codex_resume_state(current) != selected:
            raise InvalidResumeState()

    return _Conversion(
        session_dir=session_dir,
        rollout_bytes=rollout_bytes,
        manifest_bytes=encode_native_resume_state(legacy_state),
        write_slot=write_slot,
        write_manifest=write_manifest,
        write_owner=write_owner,
        legacy_metadata=legacy_metadata,
    )


def _real_directory(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError:
        raise InvalidResumeState() from None
    if not stat.S_ISDIR(mode):
        raise InvalidResumeState()


def _conversion_plans(
    quest_dir: Path,
) -> tuple[list[_Conversion], list[Path]]:
    steps = quest_dir / "steps"
    if not _entry_exists(steps):
        return [], []
    _real_directory(steps)

    plans: list[_Conversion] = []
    recovery_directories: list[Path] = []
    try:
        phases = sorted(steps.iterdir(), key=lambda path: path.name)
        for phase_dir in phases:
            _real_directory(phase_dir)
            sessions = sorted(
                phase_dir.iterdir(), key=lambda path: path.name)
            for session_dir in sessions:
                if ".attempt-" in session_dir.name:
                    continue
                _real_directory(session_dir)
                if _entry_exists(session_dir / _LEGACY_METADATA):
                    plan = _preflight_legacy(session_dir)
                    plans.append(plan)
                    if not plan.write_slot:
                        recovery_directories.append(session_dir)
                elif _validate_current_state(session_dir):
                    recovery_directories.append(session_dir)
    except OSError:
        raise InvalidResumeState() from None
    return plans, recovery_directories


def migrate_live_codex_resume_state(quest_dir: Path) -> None:
    plans, recovery_directories = _conversion_plans(quest_dir)

    for session_dir in recovery_directories:
        durable.fsync_directory(session_dir)

    for plan in plans:
        if plan.write_slot:
            durable.durable_replace_bytes(
                plan.session_dir / CODEX_ROLLOUT_SLOTS[0],
                plan.rollout_bytes,
            )
        if plan.write_manifest:
            durable.durable_replace_bytes(
                plan.session_dir / RESUME_STATE_NAME,
                plan.manifest_bytes,
            )
        if plan.write_owner:
            durable.durable_replace_bytes(
                plan.session_dir / "provider", b"codex")
        plan.legacy_metadata.unlink()
        durable.fsync_directory(plan.session_dir)
