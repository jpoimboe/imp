# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Provider-neutral persistence for native provider resume state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from imp.providers.resolve import provider_is_registered

RESUME_STATE_NAME = "resume-state.json"


class InvalidResumeState(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class NativeResumeState:
    provider: str
    state: dict[str, Any]


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidResumeState()
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise InvalidResumeState()


def parse_native_resume_state(raw: bytes) -> NativeResumeState:
    try:
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InvalidResumeState() from None

    if not isinstance(data, dict) or set(data) != {"schema_version", "provider", "state"}:
        raise InvalidResumeState()
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise InvalidResumeState()
    provider = data["provider"]
    if not isinstance(provider, str) or not provider or not provider_is_registered(provider):
        raise InvalidResumeState()
    state = data["state"]
    if not isinstance(state, dict):
        raise InvalidResumeState()
    return NativeResumeState(provider=provider, state=state)


def read_native_resume_state(state_dir: Path) -> NativeResumeState | None:
    path = state_dir / RESUME_STATE_NAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    return parse_native_resume_state(raw)


def encode_native_resume_state(resume_state: NativeResumeState) -> bytes:
    try:
        return json.dumps(
            {
                "schema_version": 1,
                "provider": resume_state.provider,
                "state": resume_state.state,
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except ValueError:
        raise InvalidResumeState() from None
