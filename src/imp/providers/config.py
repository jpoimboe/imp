"""Parse .imp/config.yaml. Plan 1 validates structure; Plan 2 resolves to models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from imp.engine._yaml import items
from imp.engine.errors import LoadError
from imp.engine.model_rules import parse_role_constraints
from imp.engine.model_tiers import parse_model_tiers
from imp.engine.types import RoleConstraint, SourceLoc


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

    def role_names(self) -> frozenset[str]:
        return frozenset(self.roles)


def load_config(project_root: Path) -> Config:
    path = project_root / ".imp" / "config.yaml"
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
    return Config(roles=roles, source=src,
                  model_rules=model_rules, model_tiers=model_tiers)
