# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from imp.engine.archive.blobstore import hash_content
from imp.engine.codex_resume_migration import parse_legacy_codex_resume_state
from imp.engine.resume_state import (
    RESUME_STATE_NAME,
    InvalidResumeState,
    NativeResumeState,
    encode_native_resume_state,
    parse_native_resume_state,
)
from imp.providers.codex_rollout import (
    CODEX_ROLLOUT_SLOTS,
    parse_codex_resume_state,
)

_LEGACY_METADATA = "codex_thread.json"
_LEGACY_ROLLOUT_ROOT = "codex_rollout"
_PROVIDER = "provider"
_CANDIDATE_NAMES = {
    _LEGACY_METADATA,
    RESUME_STATE_NAME,
    *CODEX_ROLLOUT_SLOTS,
}


@dataclass(frozen=True, slots=True)
class ArchiveCodexResumePlan:
    step_blobs: dict[str, str]
    blob_bytes: Sequence[bytes]


def _read_blob(
    step_blobs: dict[str, str],
    path: str,
    read_blob: Callable[[str], bytes | None],
) -> bytes:
    try:
        digest = step_blobs[path]
    except KeyError:
        raise InvalidResumeState() from None
    blob = read_blob(digest)
    if blob is None or hash_content(blob) != digest:
        raise InvalidResumeState()
    return blob


def _session_prefixes(step_blobs: dict[str, str]) -> list[str]:
    prefixes: set[str] = set()
    for path in step_blobs:
        components = path.split("/")
        if (
            len(components) == 3
            and components[0]
            and components[1]
            and ".attempt-" not in components[1]
            and components[2] in _CANDIDATE_NAMES
        ):
            prefixes.add("/".join(components[:2]))
    return sorted(prefixes)


def _owner_bytes(
    step_blobs: dict[str, str],
    prefix: str,
    read_blob: Callable[[str], bytes | None],
) -> bytes:
    raw = _read_blob(step_blobs, f"{prefix}/{_PROVIDER}", read_blob)
    try:
        owner = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise InvalidResumeState() from None
    if owner != "codex":
        raise InvalidResumeState()
    return raw


def _current_plan(
    step_blobs: dict[str, str],
    prefix: str,
    read_blob: Callable[[str], bytes | None],
) -> ArchiveCodexResumePlan | None:
    manifest_path = f"{prefix}/{RESUME_STATE_NAME}"
    present_slots = [
        f"{prefix}/{slot}"
        for slot in CODEX_ROLLOUT_SLOTS
        if f"{prefix}/{slot}" in step_blobs
    ]
    if manifest_path not in step_blobs:
        if present_slots:
            raise InvalidResumeState()
        return None

    manifest_bytes = _read_blob(step_blobs, manifest_path, read_blob)
    resume_state: NativeResumeState | None
    try:
        # Unreadable, or naming a provider this install no longer registers: not codex state.
        resume_state = parse_native_resume_state(manifest_bytes)
    except InvalidResumeState:
        resume_state = None
    if resume_state is None or resume_state.provider != "codex":
        if present_slots:
            raise InvalidResumeState()
        return None

    selected = parse_codex_resume_state(resume_state)
    selected_path = f"{prefix}/{selected.rollout_slot}"
    if selected_path not in step_blobs:
        raise InvalidResumeState()

    dependencies = [
        _read_blob(step_blobs, slot_path, read_blob)
        for slot_path in present_slots
    ]
    owner = _owner_bytes(step_blobs, prefix, read_blob)
    dependencies.extend((manifest_bytes, owner))
    return ArchiveCodexResumePlan(
        step_blobs=dict(step_blobs),
        blob_bytes=tuple(dependencies),
    )


def _legacy_plan(
    step_blobs: dict[str, str],
    prefix: str,
    read_blob: Callable[[str], bytes | None],
) -> ArchiveCodexResumePlan:
    metadata_path = f"{prefix}/{_LEGACY_METADATA}"
    legacy_state = parse_legacy_codex_resume_state(
        _read_blob(step_blobs, metadata_path, read_blob)
    )
    selected = parse_codex_resume_state(legacy_state)
    rollout_path = (
        f"{prefix}/{_LEGACY_ROLLOUT_ROOT}/{selected.restore_path}"
    )
    rollout_bytes = _read_blob(step_blobs, rollout_path, read_blob)

    provider_path = f"{prefix}/{_PROVIDER}"
    if provider_path in step_blobs:
        owner_bytes = _owner_bytes(step_blobs, prefix, read_blob)
    else:
        owner_bytes = b"codex"

    slot_path = f"{prefix}/{CODEX_ROLLOUT_SLOTS[0]}"
    other_slot_path = f"{prefix}/{CODEX_ROLLOUT_SLOTS[1]}"
    manifest_path = f"{prefix}/{RESUME_STATE_NAME}"
    if other_slot_path in step_blobs:
        raise InvalidResumeState()

    staged_slot = slot_path in step_blobs
    if staged_slot and _read_blob(step_blobs, slot_path, read_blob) != rollout_bytes:
        raise InvalidResumeState()

    if manifest_path in step_blobs:
        if not staged_slot:
            raise InvalidResumeState()
        manifest_bytes = _read_blob(step_blobs, manifest_path, read_blob)
        current = parse_native_resume_state(manifest_bytes)
        if parse_codex_resume_state(current) != selected:
            raise InvalidResumeState()
    else:
        if staged_slot:
            raise InvalidResumeState()
        manifest_bytes = encode_native_resume_state(legacy_state)

    legacy_root = f"{prefix}/{_LEGACY_ROLLOUT_ROOT}/"
    migrated = {
        path: digest
        for path, digest in step_blobs.items()
        if path != metadata_path and not path.startswith(legacy_root)
    }
    migrated.setdefault(slot_path, hash_content(rollout_bytes))
    migrated.setdefault(manifest_path, hash_content(manifest_bytes))
    migrated.setdefault(provider_path, hash_content(owner_bytes))
    return ArchiveCodexResumePlan(
        step_blobs=migrated,
        blob_bytes=(rollout_bytes, manifest_bytes, owner_bytes),
    )


def plan_archive_codex_resume_state(
    step_blobs: object,
    read_blob: Callable[[str], bytes | None],
) -> ArchiveCodexResumePlan | None:
    if not isinstance(step_blobs, dict) or any(
        not isinstance(path, str) or not isinstance(digest, str)
        for path, digest in step_blobs.items()
    ):
        raise InvalidResumeState()

    planned = dict(step_blobs)
    blob_bytes: list[bytes] = []
    found = False
    for prefix in _session_prefixes(step_blobs):
        metadata_path = f"{prefix}/{_LEGACY_METADATA}"
        plan: ArchiveCodexResumePlan | None
        if metadata_path in planned:
            plan = _legacy_plan(planned, prefix, read_blob)
        else:
            plan = _current_plan(planned, prefix, read_blob)
        if plan is None:
            continue
        planned = plan.step_blobs
        blob_bytes.extend(plan.blob_bytes)
        found = True
    if not found:
        return None
    return ArchiveCodexResumePlan(
        step_blobs=planned,
        blob_bytes=tuple(blob_bytes),
    )
