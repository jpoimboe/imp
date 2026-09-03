# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Per-quest variable overrides at their boundary: the CLI's `NAME=VALUE` strings and
the API's already-typed values, both checked against the workflow's own declarations
before any quest exists."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from imp.engine.expr import ExprType
    from imp.engine.types import Vars


class VarFlagError(Exception):
    """A rejected variable override; `str()` is the `imp:` line's message body."""


def _declared_vars(control_root: Path, workflow: str | None) -> tuple[str, Vars]:
    """The named (or configured default) workflow's variable declarations."""
    from imp.assets import resolve_workflow_dir
    from imp.engine.loader import load_workflow
    from imp.providers.effective import load_effective_config

    config = load_effective_config(control_root=control_root).config
    name = workflow or config.default_workflow
    loaded = load_workflow(
        resolve_workflow_dir(control_root, name),
        configured_tiers=config.tier_names(),
    )
    return name, loaded.vars


def resolve_var_flags(
    control_root: Path, *, workflow: str | None, raw: Sequence[str] | None,
) -> dict[str, object]:
    """Typed per-quest overrides from `--var NAME=VALUE` strings. No flags means
    no workflow load at all, so callers that pass none keep today's skew tolerance."""
    if not raw:
        return {}

    # Shape first, so a malformed argument reports itself even under workflow skew.
    pairs: list[tuple[str, str]] = []
    for item in raw:
        key, sep, value = item.partition("=")
        if not sep:
            raise VarFlagError(f"--var expects NAME=VALUE, got {item!r}")
        pairs.append((key, value))

    name, workflow_vars = _declared_vars(control_root, workflow)
    declared = workflow_vars.types()
    out: dict[str, object] = {}
    for key, value in pairs:
        if key not in declared:
            known = ", ".join(sorted(declared))
            tail = f"declared variables: {known}" if known else "it declares no variables"
            raise VarFlagError(f"unknown variable {key!r} for workflow {name!r}; {tail}")
        out[key] = _coerce(key, value, declared[key])
    return out


def validate_var_overrides(
    control_root: Path, *, workflow: str | None, values: Mapping[str, object] | None,
) -> dict[str, object]:
    """Already-typed per-quest overrides checked against the workflow's declarations.
    No overrides means no workflow load, mirroring `resolve_var_flags`."""
    if not values:
        return {}
    from imp.engine.errors import LoadError
    from imp.providers.config import apply_var_overrides

    _name, workflow_vars = _declared_vars(control_root, workflow)
    try:
        apply_var_overrides(workflow_vars, dict(values))
    except LoadError as e:
        raise VarFlagError(str(e)) from e
    return dict(values)


def _coerce(name: str, value: str, declared: ExprType) -> bool | int | str:
    if declared == "bool":
        if value.lower() not in ("true", "false"):
            raise VarFlagError(
                f"variable {name!r} expects bool, got {value!r}; use true or false")
        return value.lower() == "true"
    if declared == "int":
        try:
            return int(value)
        except ValueError:
            raise VarFlagError(
                f"variable {name!r} expects int, got {value!r}") from None
    return value
