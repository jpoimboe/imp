# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""The settable-key table for `imp config set` / `unset`: where each key lives in
the document, where imp's reader reports it back, which layer owns it, and how a
command-line string is shaped into its value."""

from __future__ import annotations

import difflib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from imp.providers.config import (
    _REQUIRED_TIERS,
    _SANDBOX_BOOL_KEYS,
    _SANDBOX_LIST_KEYS,
)

Layer = Literal["project", "global", "local"]
ParseFn = Callable[[str], Any]


@dataclass(frozen=True, slots=True)
class SettableKey:
    dotted: str
    path: tuple[str, ...]
    read_path: tuple[str, ...]
    home: Layer
    project_only: bool
    parse: ParseFn


def _as_str(raw: str) -> Any:
    return raw


def _as_list(raw: str) -> Any:
    return raw.split(",")


def _as_int(raw: str) -> Any:
    return int(raw) if raw.isdigit() else raw


def _as_bool(raw: str) -> Any:
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    return raw


def _as_str_or_false(raw: str) -> Any:
    if raw.lower() == "false":
        return False
    return raw


def _as_list_or_false(raw: str) -> Any:
    if raw.lower() == "false":
        return False
    return raw.split(",")


def _as_number(raw: str) -> Any:
    if raw.isdigit():
        return int(raw)
    try:
        return float(raw)
    except ValueError:
        return raw


def _as_scalar(raw: str) -> Any:
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if raw.isdigit():
        return int(raw)
    return raw


def _k(dotted: str, path: tuple[str, ...], home: Layer, parse: ParseFn, *,
       read_path: tuple[str, ...] | None = None,
       project_only: bool = False) -> SettableKey:
    return SettableKey(dotted, path, read_path or path, home, project_only, parse)


_FIXED: tuple[SettableKey, ...] = (
    _k("default-workflow", ("default-workflow",), "project", _as_str),
    _k("providers", ("providers",), "global", _as_list),
    _k("theme", ("theme",), "global", _as_str),
    _k("editing_mode", ("editing_mode",), "global", _as_str),
    _k("test_cmd_fast", ("test_cmd_fast",), "project", _as_str_or_false),
    _k("test_cmd_slow", ("test_cmd_slow",), "project", _as_str_or_false),
    _k("test_cmd_fast_timeout", ("test_cmd_fast_timeout",), "project", _as_int),
    _k("test_cmd_slow_timeout", ("test_cmd_slow_timeout",), "project", _as_int),
    _k("pythonpath", ("pythonpath",), "project", _as_list_or_false),
    _k("max_parallel_test_runs", ("max_parallel_test_runs",), "project", _as_int),
    _k("worktree_setup", ("worktree_setup",), "project", _as_str_or_false),
    _k("context.checkpoint_threshold", ("context", "checkpoint_threshold"),
       "global", _as_number),
    _k("context.max_reincarnations", ("context", "max_reincarnations"),
       "global", _as_int),
    _k("remote.driver", ("remote", "driver"), "global", _as_str,
       read_path=("remote", "driver", "name")),
    _k("default-base", ("default-base",), "project", _as_str, project_only=True),
)

_BY_NAME: dict[str, SettableKey] = {key.dotted: key for key in _FIXED}

_SUGGESTIONS: tuple[str, ...] = (
    *(key.dotted for key in _FIXED),
    "tiers.<tier>.<provider>", "provider_envs.<provider>.env",
    "sandbox.<key>", "vars.<name>",
)


def resolve_key(dotted: str) -> SettableKey | None:
    """The table row for a dotted key, or None when it is not settable."""
    fixed = _BY_NAME.get(dotted)
    if fixed is not None:
        return fixed
    segs = tuple(dotted.split("."))
    if not all(segs):
        return None
    if len(segs) == 3 and segs[0] == "tiers" and segs[1] in _REQUIRED_TIERS:
        return _k(dotted, segs, "global", _as_str)
    if len(segs) == 3 and segs[0] == "provider_envs" and segs[2] == "env":
        return _k(dotted, segs, "global", _as_str, read_path=segs[:2])
    if len(segs) == 2 and segs[0] == "sandbox":
        if segs[1] in _SANDBOX_BOOL_KEYS:
            return _k(dotted, segs, "global", _as_bool)
        if segs[1] in _SANDBOX_LIST_KEYS:
            return _k(dotted, segs, "global", _as_list)
        return None
    if len(segs) == 2 and segs[0] == "vars":
        return _k(dotted, segs, "project", _as_scalar)
    return None


def unknown_key_message(dotted: str) -> str:
    near = difflib.get_close_matches(dotted, _SUGGESTIONS, n=1, cutoff=0.7)
    if near:
        return f"unknown setting {dotted!r} (did you mean {near[0]!r}?)"
    return f"unknown setting {dotted!r} — run 'imp config' to see the settable keys"
