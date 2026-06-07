"""Parse imp/config.yaml. Plan 1 validates structure; Plan 2 resolves to models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from imp.engine._yaml import items
from imp.engine.errors import LoadError
from imp.engine.expr import ExprType
from imp.engine.model_rules import parse_role_constraints
from imp.engine.model_tiers import parse_model_tiers
from imp.engine.types import RoleConstraint, SourceLoc, Vars


@dataclass(frozen=True, slots=True)
class ProviderModel:
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class RoleConfig:
    name: str
    prefer: tuple[ProviderModel, ...]


@dataclass(frozen=True, slots=True)
class Config:
    """`roles` is a plain dict for V0; callers must treat it as immutable.
    Plan 2 will wrap it in MappingProxyType for runtime enforcement (same
    deferral as Workflow.agents).
    """
    roles: dict[str, RoleConfig] = field(hash=False)
    source: SourceLoc = field(hash=False)
    model_rules: dict[str, RoleConstraint] = field(hash=False, default_factory=dict)
    model_tiers: tuple[str, ...] | None = field(hash=False, default=None)
    var_overrides: dict[str, object] = field(hash=False, default_factory=dict)
    lint_command: str | None = field(hash=False, default=None)
    test_command: str | None = field(hash=False, default=None)
    editing_mode: str = field(hash=False, default="emacs")

    def role_names(self) -> frozenset[str]:
        return frozenset(self.roles)


def load_config(project_root: Path) -> Config:
    path = project_root / "imp" / "config.yaml"
    if not path.exists():
        raise LoadError(
            "config.yaml not found",
            SourceLoc(file=path, line=1),
            hint="run `imp init` to create it (Plan 3) or write it by hand",
        )
    try:
        raw: Any = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise LoadError(
            f"invalid YAML in config.yaml: {e}",
            SourceLoc(file=path, line=1),
        ) from e
    if not isinstance(raw, dict) or "roles" not in raw:
        raise LoadError(
            "config.yaml must have a 'roles:' mapping",
            SourceLoc(file=path, line=1),
        )

    roles_raw = raw["roles"]
    if not isinstance(roles_raw, dict) or "default" not in roles_raw:
        raise LoadError(
            "config.yaml must define 'default' role under roles:",
            SourceLoc(file=path, line=1),
        )

    roles: dict[str, RoleConfig] = {}
    for name, spec in items(roles_raw):
        source = SourceLoc(file=path, line=1)
        if not isinstance(spec, dict) or "prefer" not in spec:
            raise LoadError(f"role {name!r} missing 'prefer' list", source)
        prefer_raw = spec["prefer"]
        if not isinstance(prefer_raw, list) or not prefer_raw:
            raise LoadError(
                f"role {name!r} prefer list must be non-empty", source
            )
        models: list[ProviderModel] = []
        for entry in prefer_raw:
            if (
                not isinstance(entry, dict)
                or "provider" not in entry
                or "model" not in entry
            ):
                raise LoadError(
                    f"role {name!r} prefer entry must have provider+model",
                    source,
                )
            models.append(
                ProviderModel(
                    provider=str(entry["provider"]),
                    model=str(entry["model"]),
                )
            )
        roles[name] = RoleConfig(name=name, prefer=tuple(models))

    src = SourceLoc(file=path, line=1)
    model_rules = parse_role_constraints(raw.get("model_rules"), path)
    model_tiers = (
        parse_model_tiers(raw["model_tiers"], src) if "model_tiers" in raw else None
    )
    var_overrides = _parse_var_overrides(raw.get("vars"), src)
    lint_command = raw.get("lint_command")
    if lint_command is not None and not isinstance(lint_command, str):
        raise LoadError(
            "'lint_command' must be a string (e.g. \"make lint\")", src
        )
    test_command = raw.get("test_command")
    if test_command is not None and not isinstance(test_command, str):
        raise LoadError(
            "'test_command' must be a string (e.g. \"make test\")", src
        )
    editing_mode = raw.get("editing_mode", "emacs")
    if editing_mode not in ("vi", "emacs"):
        raise LoadError(
            "'editing_mode' must be 'vi' or 'emacs'", src
        )
    return Config(roles=roles, source=src,
                  model_rules=model_rules, model_tiers=model_tiers,
                  var_overrides=var_overrides, lint_command=lint_command,
                  test_command=test_command, editing_mode=editing_mode)


def _parse_var_overrides(raw: Any, src: SourceLoc) -> dict[str, object]:
    """Parse a top-level `vars:` mapping of scalar overrides. Validation against
    the workflow's declarations happens later, in apply_var_overrides -- here we
    only enforce the shape (a mapping of scalars)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise LoadError("config.yaml 'vars:' must be a mapping", src)
    out: dict[str, object] = {}
    for name, value in items(raw):
        if not isinstance(value, (bool, int, str)):
            raise LoadError(
                f"config.yaml vars: {name!r} must be a bool, int, or string", src
            )
        out[str(name)] = value
    return out


def _matches_type(value: object, declared: ExprType) -> bool:
    # bool is a subclass of int, so order the checks bool-before-int.
    if declared == "bool":
        return isinstance(value, bool)
    if declared == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, str)


def apply_var_overrides(
    workflow_vars: Vars, overrides: dict[str, object],
) -> dict[str, object]:
    """Workflow var defaults with config overrides applied on top. Every override
    key must be declared by the workflow and type-match its declaration; an
    unknown var or a type mismatch is a LoadError (spec §11)."""
    result = dict(workflow_vars.defaults())
    declared = workflow_vars.types()
    decls = workflow_vars.decls
    fallback = decls[0].source if decls else SourceLoc(file=Path("config.yaml"), line=1)
    for name, value in overrides.items():
        if name not in declared:
            raise LoadError(
                f"config.yaml vars: {name!r} is not declared by the workflow",
                fallback,
            )
        if not _matches_type(value, declared[name]):
            raise LoadError(
                f"config.yaml vars: {name!r} expects {declared[name]}, "
                f"got {type(value).__name__}",
                next(d.source for d in decls if d.name == name),
            )
        result[name] = value
    return result
