# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Parse a single config layer: the project, global, or local config file.
Schema (post 2026-06-04-roles-to-tiers):

    providers: [claude, openai]           # ordered preference list
    tiers:
      low:    {claude: haiku,  openai: gpt-6-luna}
      medium: {claude: sonnet, openai: gpt-6.1-terra}
      high:   {claude: opus,   openai: gpt-6.1-sol}
    provider_envs:                        # optional; default <PROVIDER>_API_KEY
      openai: {env: OPENAI_API_KEY}
    test_cmd_fast: "..."                  # cheap gate (lint+typecheck+fast); false if none needed
    test_cmd_slow: "..."                  # full run once per quest; false if none needed
    test_cmd_fast_timeout: 120            # optional fast-gate ceiling, seconds (default 120)
    test_cmd_slow_timeout: 300            # optional slow-gate ceiling, seconds (default 300)
    worktree_setup: uv sync --all-extras  # runs in each new quest worktree; false if none needed
    pythonpath: [src]                     # import roots on PYTHONPATH; false if none needed
    default-workflow: default             # workflow new quests use (default "default")
    max_parallel_test_runs: 1             # max concurrent verify-gate runs (default 1)
    editing_mode: emacs                   # vi | emacs (default emacs)
    vars: {...}                           # optional scalar overrides

The four stack-dependent settings above — test_cmd_fast, test_cmd_slow, worktree_setup,
pythonpath — share one three-state shape: an absent key or an explicit YAML null is "never
decided", `false` is "this project needs none", anything else is the value. `""` and `[]`
are rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ruamel.yaml.error import YAMLError

from imp.engine._yaml import config_yaml_error, items, load_config_yaml
from imp.engine.errors import LoadError
from imp.engine.expr import ExprType
from imp.engine.types import SourceLoc, Vars

_REQUIRED_TIERS: tuple[str, ...] = ("high", "medium", "low")

_COMMAND_KEY_EXAMPLES: dict[str, str] = {
    "test_cmd_fast": '"ruff check . && pytest -q"',
    "test_cmd_slow": '"pytest"',
    "worktree_setup": '"uv sync --all-extras"',
}


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
    test_cmd_fast: str | Literal[False] | None = field(hash=False, default=None)
    test_cmd_slow: str | Literal[False] | None = field(hash=False, default=None)
    test_cmd_fast_timeout: int | None = field(hash=False, default=None)
    test_cmd_slow_timeout: int | None = field(hash=False, default=None)
    worktree_setup: str | Literal[False] | None = field(hash=False, default=None)
    pythonpath: tuple[str, ...] | Literal[False] | None = field(
        hash=False, default=None)
    max_parallel_test_runs: int = field(hash=False, default=1)
    editing_mode: str = field(hash=False, default="emacs")
    theme: str = field(hash=False, default="ansi")
    default_workflow: str = field(hash=False, default="default")
    sandbox: dict[str, Any] = field(hash=False, default_factory=dict)
    remote: dict[str, Any] = field(hash=False, default_factory=dict)
    context: dict[str, Any] = field(hash=False, default_factory=dict)

    def tier_names(self) -> frozenset[str]:
        return frozenset(self.tiers)

    def env_for(self, provider: str) -> str:
        """Env-var name for `provider` — `provider_envs[provider]` if present,
        else `<PROVIDER>_API_KEY`."""
        return self.provider_envs.get(provider, f"{provider.upper()}_API_KEY")

    def context_max_reincarnations(self) -> int:
        reincarnations: int = self.context.get("max_reincarnations", 2)
        return reincarnations


def validate_workflow_name(name: str) -> bool:
    """A workflow name is a single path segment: non-blank, no '/' or '\\',
    no leading '.'."""
    return (
        bool(name.strip())
        and "/" not in name
        and "\\" not in name
        and not name.startswith(".")
    )


def parse_default_base(path: Path) -> str | None:
    """Read imp/config.yaml `default-base:` field — the integration branch a
    quest is based on. Absent file or missing key → None; `imp new` treats that
    as a hard error (there is no runtime auto-detect fallback). Read directly
    from the project layer, not merged into the layered Config. A branch name may
    contain '/', so it is not validated as a single path segment — only that,
    when present, it is a non-blank string."""
    if not path.exists():
        return None
    try:
        raw: Any = load_config_yaml(path.read_text()) or {}
    except YAMLError as e:
        raise LoadError(
            f"invalid YAML in {path.name}: {config_yaml_error(e)}", SourceLoc(file=path, line=1)
        ) from e
    if not isinstance(raw, dict):
        raise LoadError(f"{path.name} root must be a mapping", SourceLoc(file=path, line=1))
    base = raw.get("default-base")
    if base is None:
        return None
    if not isinstance(base, str) or not base.strip():
        raise LoadError(
            "'default-base' must be a non-blank branch name",
            SourceLoc(file=path, line=1),
        )
    return base


def parse_config_layer(path: Path) -> tuple[dict[str, Any], SourceLoc] | None:
    """Parse ONE config-layer file into a dict of only the keys it sets (spec §2
    merge). Returns None if the file is absent. Tiers/providers may be omitted —
    the provider-code floor fills them at merge time. Each present key is type-
    validated by the per-key helpers below."""
    if not path.exists():
        return None
    try:
        raw: Any = load_config_yaml(path.read_text()) or {}
    except YAMLError as e:
        raise LoadError(
            f"invalid YAML in {path.name}: {config_yaml_error(e)}", SourceLoc(file=path, line=1)
        ) from e
    if not isinstance(raw, dict):
        raise LoadError(f"{path.name} root must be a mapping", SourceLoc(file=path, line=1))
    src = SourceLoc(file=path, line=1)
    out: dict[str, Any] = {}
    # Use `.get(...) is not None`, not `in raw`: a key written with no value
    # (`providers:` on its own line) parses to key-present/value-None. Treat that
    # as absent — the floor/merge fills it — instead of passing None down to
    # _parse_providers, which would raise the misleading "must define a
    # 'providers:' list" message from a project- or local-layer file (M1).
    if raw.get("providers") is not None:
        out["providers"] = _parse_providers(raw["providers"], src)
    if raw.get("tiers") is not None:
        out["tiers"] = _parse_tiers(raw["tiers"], src)
    if raw.get("provider_envs") is not None:
        out["provider_envs"] = _parse_provider_envs(raw["provider_envs"], src)
    if raw.get("vars") is not None:
        out["vars"] = _parse_var_overrides(raw["vars"], src)
    if "default-workflow" in raw:
        v = raw["default-workflow"]
        if not isinstance(v, str) or not validate_workflow_name(v):
            raise LoadError(
                "'default-workflow' must be a single workflow name "
                "(no path separators)", src)
        out["default-workflow"] = v
    # `.get(...) is not None`: a valueless `test_cmd_fast:` is YAML null and reads
    # as never-decided, exactly like an absent key.
    for cmd_key, example in _COMMAND_KEY_EXAMPLES.items():
        if raw.get(cmd_key) is not None:
            v = raw[cmd_key]
            if v is not False and (not isinstance(v, str) or not v):
                raise LoadError(
                    f"'{cmd_key}' must be a non-empty string (e.g. {example}) "
                    "or false to declare this project needs none", src)
            out[cmd_key] = v
    for timeout_key in ("test_cmd_fast_timeout", "test_cmd_slow_timeout"):
        if timeout_key in raw:
            v = raw[timeout_key]
            # bool is an int subclass; reject it so `..._timeout: true` isn't read as 1.
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise LoadError(
                    f"'{timeout_key}' must be a positive integer "
                    "(seconds, e.g. 120)", src)
            out[timeout_key] = v
    if raw.get("pythonpath") is not None:
        v = raw["pythonpath"]
        if v is not False and (
            not isinstance(v, list) or not v
            or not all(isinstance(e, str) and e for e in v)
        ):
            raise LoadError(
                "'pythonpath' must be a non-empty list of worktree-relative "
                "directory names (e.g. [src]) or false to declare this project "
                "needs none", src)
        out["pythonpath"] = v if v is False else list(v)
    if "max_parallel_test_runs" in raw:
        v = raw["max_parallel_test_runs"]
        # bool is an int subclass; reject it so `max_parallel_test_runs: true` isn't read as 1.
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise LoadError(
                "'max_parallel_test_runs' must be a positive integer "
                "(concurrent verify-gate runs, e.g. 1)", src)
        out["max_parallel_test_runs"] = v
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
    if raw.get("context") is not None:
        out["context"] = _parse_context(raw["context"], src)
    return out, src


def _parse_providers(raw: Any, src: SourceLoc) -> tuple[str, ...]:
    if raw is None:
        raise LoadError(f"{src.file.name} must define a 'providers:' list", src)
    if not isinstance(raw, list) or not raw:
        raise LoadError(
            f"{src.file.name} 'providers:' must be a non-empty list of provider names",
            src,
            hint="run 'imp config set providers claude' to fix",
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
_DRIVER_KNOWN_KEYS = {"name", "events", "startup_timeout"}
_SANDBOX_LIST_KEYS = (
    "secret_paths", "writable_caches", "extra_readable", "extra_writable", "env_passthrough",
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
    return {"driver": out}


def _parse_context(raw: Any, src: SourceLoc) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise LoadError(f"{src.file.name} 'context:' must be a mapping", src)
    out: dict[str, Any] = {}
    for k, v in items(raw):
        if k == "max_reincarnations":
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise LoadError(
                    "context.max_reincarnations must be a non-negative integer", src)
            out[k] = v
        else:
            raise LoadError(
                f"context.{k!r} is not a known context setting "
                "(allowed: max_reincarnations)", src)
    return out


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
