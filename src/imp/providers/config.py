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
    providers: tuple[str, ...] = field(hash=False, default=())
    tiers: dict[str, dict[str, str]] = field(hash=False, default_factory=dict)
    provider_envs: dict[str, str] = field(hash=False, default_factory=dict)
    source: SourceLoc = field(
        hash=False,
        default_factory=lambda: SourceLoc(file=Path("config.yaml"), line=1),
    )
    var_overrides: dict[str, object] = field(hash=False, default_factory=dict)
    lint_command: str | None = field(hash=False, default=None)
    test_command: str | None = field(hash=False, default=None)
    editing_mode: str = field(hash=False, default="emacs")
    theme: str = field(hash=False, default="ansi")

    def tier_names(self) -> frozenset[str]:
        return frozenset(self.tiers)

    def env_for(self, provider: str) -> str:
        """Env-var name for `provider` — `provider_envs[provider]` if present,
        else `<PROVIDER>_API_KEY`."""
        return self.provider_envs.get(provider, f"{provider.upper()}_API_KEY")


def parse_config_layer(path: Path) -> tuple[dict[str, Any], SourceLoc] | None:
    """Parse ONE config-layer file into a dict of only the keys it sets (spec §2
    merge). Returns None if the file is absent. Tiers/providers may be omitted —
    the provider-code floor fills them at merge time. Each present key is type-
    validated by the per-key helpers below."""
    if not path.exists():
        return None
    try:
        raw: Any = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise LoadError(f"invalid YAML in {path.name}: {e}", SourceLoc(file=path, line=1)) from e
    if not isinstance(raw, dict):
        raise LoadError(f"{path.name} root must be a mapping", SourceLoc(file=path, line=1))
    src = SourceLoc(file=path, line=1)
    out: dict[str, Any] = {}
    # Use `.get(...) is not None`, not `in raw`: a key written with no value
    # (`providers:` on its own line) parses to key-present/value-None. Treat that
    # as absent — the floor/merge fills it — instead of passing None down to
    # _parse_providers, which would raise the misleading "config.yaml must define
    # a 'providers:' list" message from a project.yaml/local-layer file (M1).
    if raw.get("providers") is not None:
        out["providers"] = _parse_providers(raw["providers"], src)
    if raw.get("tiers") is not None:
        out["tiers"] = _parse_tiers(raw["tiers"], src)
    if raw.get("provider_envs") is not None:
        out["provider_envs"] = _parse_provider_envs(raw["provider_envs"], src)
    if raw.get("vars") is not None:
        out["vars"] = _parse_var_overrides(raw["vars"], src)
    if "lint_command" in raw:
        v = raw["lint_command"]
        if not isinstance(v, str):
            raise LoadError("'lint_command' must be a string (e.g. \"make lint\")", src)
        out["lint_command"] = v
    if "test_command" in raw:
        v = raw["test_command"]
        if not isinstance(v, str):
            raise LoadError("'test_command' must be a string (e.g. \"make test\")", src)
        out["test_command"] = v
    if "editing_mode" in raw:
        v = raw["editing_mode"]
        if v not in ("vi", "emacs"):
            raise LoadError("'editing_mode' must be 'vi' or 'emacs'", src)
        out["editing_mode"] = v
    if "theme" in raw:
        v = raw["theme"]
        if v not in ("dark", "light", "ansi"):
            raise LoadError("'theme' must be 'dark', 'light', or 'ansi'", src)
        out["theme"] = v
    return out, src


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
        return {}
    if not isinstance(raw, dict):
        raise LoadError("config 'tiers:' must be a mapping", src)
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
