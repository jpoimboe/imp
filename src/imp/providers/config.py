"""Parse imp/config.yaml. Schema (post 2026-06-04-roles-to-tiers):

    providers: [claude_code, openai]      # ordered preference list
    tiers:
      low:    {claude_code: haiku,  openai: gpt-5.4-mini}
      medium: {claude_code: sonnet, openai: gpt-5.3-codex}
      high:   {claude_code: opus,   openai: gpt-5.5}
    provider_envs:                        # optional; default <PROVIDER>_API_KEY
      openai: {env: OPENAI_API_KEY}
    lint_command: "..."                   # optional
    test_command: "..."                   # optional
    editing_mode: emacs                   # vi | emacs (default emacs)
    vars: {...}                           # optional scalar overrides
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from imp.engine._yaml import items
from imp.engine.errors import LoadError
from imp.engine.expr import ExprType
from imp.engine.types import SourceLoc, Vars

_REQUIRED_TIERS: tuple[str, ...] = ("low", "medium", "high")


@dataclass(frozen=True, slots=True)
class ProviderModel:
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class Config:
    providers: tuple[str, ...] = field(hash=False)
    tiers: dict[str, dict[str, str]] = field(hash=False)
    provider_envs: dict[str, str] = field(hash=False, default_factory=dict)
    source: SourceLoc = field(
        hash=False,
        default_factory=lambda: SourceLoc(file=Path("config.yaml"), line=1),
    )
    var_overrides: dict[str, object] = field(hash=False, default_factory=dict)
    lint_command: str | None = field(hash=False, default=None)
    test_command: str | None = field(hash=False, default=None)
    editing_mode: str = field(hash=False, default="emacs")

    def tier_names(self) -> frozenset[str]:
        return frozenset(self.tiers)

    def env_for(self, provider: str) -> str:
        """Env-var name for `provider` — `provider_envs[provider]` if present,
        else `<PROVIDER>_API_KEY`."""
        return self.provider_envs.get(provider, f"{provider.upper()}_API_KEY")


def load_config(project_root: Path) -> Config:
    path = project_root / "imp" / "config.yaml"
    if not path.exists():
        raise LoadError(
            "config.yaml not found",
            SourceLoc(file=path, line=1),
            hint="run `imp init` to create it",
        )
    try:
        raw: Any = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise LoadError(
            f"invalid YAML in config.yaml: {e}",
            SourceLoc(file=path, line=1),
        ) from e
    if not isinstance(raw, dict):
        raise LoadError(
            "config.yaml root must be a mapping",
            SourceLoc(file=path, line=1),
        )
    src = SourceLoc(file=path, line=1)

    providers = _parse_providers(raw.get("providers"), src)
    tiers = _parse_tiers(raw.get("tiers"), src)
    provider_envs = _parse_provider_envs(raw.get("provider_envs"), src)

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
        raise LoadError("'editing_mode' must be 'vi' or 'emacs'", src)

    return Config(
        providers=providers, tiers=tiers, provider_envs=provider_envs,
        source=src, var_overrides=var_overrides, lint_command=lint_command,
        test_command=test_command, editing_mode=editing_mode,
    )


def _parse_providers(raw: Any, src: SourceLoc) -> tuple[str, ...]:
    if raw is None:
        raise LoadError("config.yaml must define a 'providers:' list", src)
    if not isinstance(raw, list) or not raw:
        raise LoadError(
            "config.yaml 'providers:' must be a non-empty list of provider names",
            src,
        )
    out: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str):
            raise LoadError(
                f"providers entry must be a string, got {type(entry).__name__}", src,
            )
        if entry in seen:
            raise LoadError(f"providers has duplicate entry {entry!r}", src)
        seen.add(entry)
        out.append(entry)
    return tuple(out)


def _parse_tiers(raw: Any, src: SourceLoc) -> dict[str, dict[str, str]]:
    if raw is None:
        raise LoadError("config.yaml must define a 'tiers:' mapping", src)
    if not isinstance(raw, dict):
        raise LoadError("config.yaml 'tiers:' must be a mapping", src)
    missing = [t for t in _REQUIRED_TIERS if t not in raw]
    if missing:
        raise LoadError(
            f"config.yaml 'tiers:' must define {list(_REQUIRED_TIERS)}; "
            f"missing: {missing}",
            src,
        )
    out: dict[str, dict[str, str]] = {}
    for tier_name, entry in items(raw):
        if tier_name not in _REQUIRED_TIERS:
            raise LoadError(
                f"tiers: {tier_name!r} is not a configurable tier "
                f"(must be one of {list(_REQUIRED_TIERS)})",
                src,
            )
        if not isinstance(entry, dict) or not entry:
            raise LoadError(
                f"tiers.{tier_name}: must be a non-empty mapping of "
                f"provider to model", src,
            )
        models: dict[str, str] = {}
        for prov, model in items(entry):
            if not isinstance(prov, str) or not isinstance(model, str):
                raise LoadError(
                    f"tiers.{tier_name}: entries must be string provider→model "
                    f"(got {prov!r}: {model!r})",
                    src,
                )
            models[prov] = model
        out[str(tier_name)] = models
    return out


def _parse_provider_envs(raw: Any, src: SourceLoc) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise LoadError("config.yaml 'provider_envs:' must be a mapping", src)
    out: dict[str, str] = {}
    for prov, spec in items(raw):
        if not isinstance(spec, dict) or "env" not in spec or not isinstance(spec["env"], str):
            raise LoadError(
                f"provider_envs[{prov!r}] must be a mapping with a string 'env' key",
                src,
            )
        out[str(prov)] = str(spec["env"])
    return out


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
                f"config.yaml vars: {name!r} must be a bool, int, or string", src,
            )
        out[str(name)] = value
    return out


def _matches_type(value: object, declared: ExprType) -> bool:
    if declared == "bool":
        return isinstance(value, bool)
    if declared == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, str)


def apply_var_overrides(
    workflow_vars: Vars, overrides: dict[str, object],
) -> dict[str, object]:
    """Workflow var defaults with config overrides applied on top. Every override
    key must be declared by the workflow and type-match its declaration."""
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
