"""Parse a single config layer (imp/project.yaml, user, or local config.yaml).
Schema (post 2026-06-04-roles-to-tiers):

    providers: [claude_code, openai]      # ordered preference list
    tiers:
      low:    {claude_code: haiku,  openai: gpt-5.4-mini}
      medium: {claude_code: sonnet, openai: gpt-5.3-codex}
      high:   {claude_code: opus,   openai: gpt-5.5}
    provider_envs:                        # optional; default <PROVIDER>_API_KEY
      openai: {env: OPENAI_API_KEY}
    verify_command: "..."                 # optional combined verify gate (tests+lint+typecheck)
    editing_mode: emacs                   # vi | emacs (default emacs)
    vars: {...}                           # optional scalar overrides
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from imp.engine._yaml import LineLoader, items, key_loc, loc
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
    verify_command: str | None = field(hash=False, default=None)
    editing_mode: str = field(hash=False, default="emacs")
    theme: str = field(hash=False, default="ansi")
    sandbox: dict[str, Any] = field(hash=False, default_factory=dict)
    remote: dict[str, Any] = field(hash=False, default_factory=dict)
    project_facts: dict[str, str] = field(hash=False, default_factory=dict)
    guidance: str = field(hash=False, default="")

    def tier_names(self) -> frozenset[str]:
        return frozenset(self.tiers)

    def env_for(self, provider: str) -> str:
        """Env-var name for `provider` — `provider_envs[provider]` if present,
        else `<PROVIDER>_API_KEY`."""
        return self.provider_envs.get(provider, f"{provider.upper()}_API_KEY")


_FACT_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
_FACT_SCALARS = (str, int, float, bool)


def parse_project_facts(path: Path) -> tuple[dict[str, str], str]:
    """Read imp/project.yaml `facts:` and `guidance:` into (facts, guidance)."""
    if not path.exists():
        return {}, ""
    try:
        raw: Any = yaml.load(path.read_text(), Loader=LineLoader) or {}
    except yaml.YAMLError as e:
        raise LoadError(
            f"invalid YAML in {path.name}: {e}", SourceLoc(file=path, line=1)
        ) from e
    if not isinstance(raw, dict):
        raise LoadError(f"{path.name} root must be a mapping", SourceLoc(file=path, line=1))

    guidance_raw = raw.get("guidance")
    if guidance_raw is None:
        guidance = ""
    elif not isinstance(guidance_raw, _FACT_SCALARS):
        raise LoadError(
            "'guidance:' must be a string scalar",
            key_loc(path, raw, "guidance"),
        )
    else:
        guidance = str(guidance_raw)

    facts: dict[str, str] = {}
    facts_raw = raw.get("facts")
    if facts_raw is None:
        return facts, guidance
    if not isinstance(facts_raw, dict):
        raise LoadError(
            "'facts:' must be a mapping of name to scalar value",
            key_loc(path, raw, "facts"),
        )
    facts_src = loc(path, facts_raw)
    for key, value in items(facts_raw):
        if not isinstance(key, str) or not _FACT_KEY_RE.match(key):
            raise LoadError(
                f"facts key {key!r} is not a valid identifier "
                "([A-Za-z][A-Za-z0-9_]*)",
                facts_src,
            )
        if key == "guidance":
            raise LoadError(
                "facts key 'guidance' is reserved (it is the sibling top-level "
                "'guidance:' field)",
                facts_src,
            )
        if not isinstance(value, _FACT_SCALARS):
            raise LoadError(
                f"facts.{key}: value must be a scalar, not {type(value).__name__}",
                loc(path, value) if isinstance(value, dict) else facts_src,
            )
        facts[key] = ("true" if value else "false") if isinstance(value, bool) else str(value)
    return facts, guidance


def parse_default_workflow(path: Path) -> str:
    """Read imp/project.yaml `default-workflow:` field. Absent → 'imp'. Read
    directly from the project layer, not merged into the layered Config.
    The value is a single path segment (no separators / parent escapes)."""
    if not path.exists():
        return "imp"
    try:
        raw: Any = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise LoadError(
            f"invalid YAML in {path.name}: {e}", SourceLoc(file=path, line=1)
        ) from e
    if not isinstance(raw, dict):
        raise LoadError(f"{path.name} root must be a mapping", SourceLoc(file=path, line=1))
    name = raw.get("default-workflow")
    if name is None:
        return "imp"
    if (not isinstance(name, str) or not name.strip() or "/" in name or "\\" in name
            or name.startswith(".")):
        raise LoadError(
            "'default-workflow' must be a single workflow name "
            "(no path separators)",
            SourceLoc(file=path, line=1),
        )
    return name


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
    # _parse_providers, which would raise the misleading "must define a
    # 'providers:' list" message from a project.yaml/local-layer file (M1).
    if raw.get("providers") is not None:
        out["providers"] = _parse_providers(raw["providers"], src)
    if raw.get("tiers") is not None:
        out["tiers"] = _parse_tiers(raw["tiers"], src)
    if raw.get("provider_envs") is not None:
        out["provider_envs"] = _parse_provider_envs(raw["provider_envs"], src)
    if raw.get("vars") is not None:
        out["vars"] = _parse_var_overrides(raw["vars"], src)
    if "verify_command" in raw:
        v = raw["verify_command"]
        if not isinstance(v, str):
            raise LoadError("'verify_command' must be a string (e.g. \"make check\")", src)
        out["verify_command"] = v
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
    if raw.get("sandbox") is not None:
        out["sandbox"] = _parse_sandbox(raw["sandbox"], src)
    if raw.get("remote") is not None:
        out["remote"] = _parse_remote(raw["remote"], src)
    return out, src


def _parse_providers(raw: Any, src: SourceLoc) -> tuple[str, ...]:
    if raw is None:
        raise LoadError(f"{src.file.name} must define a 'providers:' list", src)
    if not isinstance(raw, list) or not raw:
        raise LoadError(
            f"{src.file.name} 'providers:' must be a non-empty list of provider names",
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
        raise LoadError(f"{src.file.name} 'tiers:' must be a mapping", src)
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
        raise LoadError(f"{src.file.name} 'provider_envs:' must be a mapping", src)
    out: dict[str, str] = {}
    for prov, spec in items(raw):
        if not isinstance(spec, dict) or "env" not in spec or not isinstance(spec["env"], str):
            raise LoadError(
                f"provider_envs[{prov!r}] must be a mapping with a string 'env' key",
                src,
            )
        out[str(prov)] = str(spec["env"])
    return out


_SANDBOX_BOOL_KEYS = ("enabled", "allow_unsandboxed", "network")
_DRIVER_KNOWN_KEYS = {"name", "events", "startup_timeout", "reply_timeout"}
_SANDBOX_LIST_KEYS = (
    "secret_paths", "writable_caches", "extra_readable", "extra_writable", "extra_env",
)


def _parse_sandbox(raw: Any, src: SourceLoc) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise LoadError(f"{src.file.name} 'sandbox:' must be a mapping", src)
    out: dict[str, Any] = {}
    for k, v in items(raw):
        if k in _SANDBOX_BOOL_KEYS:
            if not isinstance(v, bool):
                raise LoadError(f"sandbox.{k} must be a boolean", src)
            out[k] = v
        elif k in _SANDBOX_LIST_KEYS:
            if not isinstance(v, list) or not all(isinstance(e, str) for e in v):
                raise LoadError(f"sandbox.{k} must be a list of strings", src)
            out[k] = list(v)
        else:
            raise LoadError(
                f"sandbox.{k!r} is not a known sandbox setting "
                f"(allowed: {list(_SANDBOX_BOOL_KEYS + _SANDBOX_LIST_KEYS)})", src)
    return out


def _parse_remote(raw: Any, src: SourceLoc) -> dict[str, Any]:
    from imp.remote.drivers import REGISTRY

    if not isinstance(raw, dict):
        raise LoadError(f"{src.file.name} 'remote:' must be a mapping", src)
    for k in raw:
        if k != "driver":
            raise LoadError(
                f"remote.{k!r} is not a known remote setting (only 'driver')", src)
    driver = raw.get("driver")
    if driver is None:
        return {}

    overrides: dict[str, Any] = {}
    if isinstance(driver, str):
        name = driver
    elif isinstance(driver, dict):
        for k in driver:
            if k not in _DRIVER_KNOWN_KEYS:
                raise LoadError(
                    f"remote.driver.{k!r} is not a known driver setting "
                    f"(allowed: {sorted(_DRIVER_KNOWN_KEYS)})", src)
        raw_name = driver.get("name")
        if not isinstance(raw_name, str) or not raw_name:
            raise LoadError(
                "remote.driver requires a non-empty string 'name'", src)
        name = raw_name
        overrides = driver
    else:
        raise LoadError(
            f"{src.file.name} 'remote.driver:' must be a built-in name string "
            "or a mapping with a 'name'", src)

    if name not in REGISTRY:
        raise LoadError(
            f"unknown remote driver {name!r} "
            f"(available built-ins: {sorted(REGISTRY)})", src)

    out: dict[str, Any] = {"name": name}
    if "events" in overrides:
        events = overrides["events"]
        if (not isinstance(events, list) or not events
                or not all(isinstance(e, str) for e in events)):
            raise LoadError(
                "remote.driver.events must be a non-empty list of strings (or [all])",
                src)
        out["events"] = list(events)
    if "startup_timeout" in overrides:
        st = overrides["startup_timeout"]
        if not isinstance(st, (int, float)) or isinstance(st, bool) or st <= 0:
            raise LoadError(
                "remote.driver.startup_timeout must be a positive number of seconds",
                src)
        out["startup_timeout"] = float(st)
    if "reply_timeout" in overrides:
        rt = overrides["reply_timeout"]
        if rt is not None and (
                not isinstance(rt, (int, float)) or isinstance(rt, bool) or rt <= 0):
            raise LoadError(
                "remote.driver.reply_timeout must be null or a positive number", src)
        out["reply_timeout"] = float(rt) if rt is not None else None
    return {"driver": out}


def _parse_var_overrides(raw: Any, src: SourceLoc) -> dict[str, object]:
    """Parse a top-level `vars:` mapping of scalar overrides. Validation against
    the workflow's declarations happens later, in apply_var_overrides -- here we
    only enforce the shape (a mapping of scalars)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise LoadError(f"{src.file.name} 'vars:' must be a mapping", src)
    out: dict[str, object] = {}
    for name, value in items(raw):
        if not isinstance(value, (bool, int, str)):
            raise LoadError(
                f"{src.file.name} vars: {name!r} must be a bool, int, or string", src,
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
                f"vars override {name!r} is not declared by the workflow",
                fallback,
            )
        if not _matches_type(value, declared[name]):
            raise LoadError(
                f"vars override {name!r} expects {declared[name]}, "
                f"got {type(value).__name__}",
                next(d.source for d in decls if d.name == name),
            )
        result[name] = value
    return result
