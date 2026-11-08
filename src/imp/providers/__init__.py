# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""V1 provider registry. Importing this package registers NOTHING: the builtins
land in providers.resolve._PROVIDERS on the first registry read, via
resolve._ensure_builtins(). Registration binds lazy thunks over BUILTIN_FLOOR, so
no registry read imports a provider module — not `imp --help`, not a dispatching
command's effective-config read — and with them no httpx (and no
rich/click/pygments behind httpx's CLI extra). A provider module loads only when
its env_check or factory is actually called.

Registration is idempotent — re-running it is a no-op for already-registered
names (resolve.register_provider rebinds, which is harmless in practice; test
isolation is owned by conftest's autouse _reset_provider_registry fixture).
"""

from __future__ import annotations

import importlib
import json
import os
import platform
from enum import Enum, auto
from shutil import which
from typing import TYPE_CHECKING, NamedTuple, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from imp.providers.base import Provider
    from imp.providers.config import Config


class BuiltinFloor(NamedTuple):
    """One builtin's registration data: module name plus floor metadata for register_provider."""
    module: str
    tier_defaults: dict[str, str] | None
    env_var: str | None
    auth_hint: str


# Pinned floors: OpenAI and Codex both use luna < terra < sol.
BUILTIN_FLOOR: dict[str, BuiltinFloor] = {
    "anthropic": BuiltinFloor(
        module="anthropic",
        tier_defaults={
            "low": "claude-haiku-5-5",
            "medium": "claude-sonnet-5-5",
            "high": "claude-opus-5-5",
        },
        env_var="ANTHROPIC_API_KEY",
        auth_hint="set ANTHROPIC_API_KEY",
    ),
    "openai": BuiltinFloor(
        module="openai",
        tier_defaults={
            "low": "gpt-6-luna",
            "medium": "gpt-6.1-terra",
            "high": "gpt-6.1-sol",
        },
        env_var="OPENAI_API_KEY",
        auth_hint="set OPENAI_API_KEY",
    ),
    "claude": BuiltinFloor(
        module="claude",
        tier_defaults={
            "low": "claude-haiku-5-5",
            "medium": "claude-sonnet-5-5",
            "high": "claude-opus-5-5",
        },
        env_var=None,  # CLI-authenticated (claude login); no env var
        auth_hint="run `claude login`",
    ),
    "codex": BuiltinFloor(
        module="codex",
        tier_defaults={
            "low": "gpt-6-luna",
            "medium": "gpt-6.1-terra",
            "high": "gpt-6.1-sol",
        },
        env_var=None,  # CLI-authenticated (codex login); no env var
        auth_hint="run `codex login`",
    ),
    # Duplicates scripted.ENV_VAR (read at runtime there); a test pins the two equal.
    "scripted": BuiltinFloor(
        module="scripted", tier_defaults=None, env_var="IMP_SCRIPTED_SCRIPT",
        auth_hint="set IMP_SCRIPTED_SCRIPT",
    ),
}


def auth_hint_lines(names: Iterable[str]) -> list[str]:
    """Indented `  <name> — <hint>` per name; a name outside the floor gets no line."""
    return [f"  {n} — {BUILTIN_FLOOR[n].auth_hint}" for n in names if n in BUILTIN_FLOOR]


class Readiness(Enum):
    """One provider's local, read-only sign-in verdict."""

    SIGNED_IN = auto()
    NOT_SIGNED_IN = auto()
    CLI_NOT_FOUND = auto()
    KEY_SET = auto()
    KEY_UNSET = auto()

    @property
    def ready(self) -> bool:
        return self in (Readiness.SIGNED_IN, Readiness.KEY_SET)


class ProviderStatus(NamedTuple):
    """A verdict plus the label every surface shows for it."""
    readiness: Readiness
    label: str


def _has_claude_oauth(raw: str | None) -> bool:
    if raw is None:
        return False
    try:
        data = json.loads(raw)
    except ValueError:
        return False
    return isinstance(data, dict) and isinstance(data.get("claudeAiOauth"), dict)


def _claude_login_found(env: Mapping[str, str]) -> bool:
    """Cheapest first; the Keychain only where the SDK keeps the login (macOS, default dir)."""
    from imp.providers.paths import claude_canonical_dir

    if env.get("CLAUDE_CODE_OAUTH_TOKEN") or env.get("ANTHROPIC_API_KEY"):
        return True
    try:
        raw = (claude_canonical_dir(env) / ".credentials.json").read_text(encoding="utf-8")
    except (OSError, ValueError, RuntimeError):     # RuntimeError: no resolvable home dir
        raw = None
    if _has_claude_oauth(raw):
        return True
    if platform.system() != "Darwin" or env.get("CLAUDE_CONFIG_DIR"):
        return False
    from claude_agent_sdk._internal.session_resume import _read_keychain_credentials
    return _has_claude_oauth(_read_keychain_credentials())


def _codex_login_found(env: Mapping[str, str]) -> bool:
    from imp.providers.codex_config import detect_auth_mode
    from imp.providers.paths import codex_canonical_dir

    try:
        auth_path = codex_canonical_dir(env) / "auth.json"
    except RuntimeError:                # no resolvable home dir
        return bool(env.get("OPENAI_API_KEY"))
    return detect_auth_mode(canonical_path=auth_path, env=env) is not None


_LOGIN_FOUND: dict[str, Callable[[Mapping[str, str]], bool]] = {
    "claude": _claude_login_found,
    "codex": _codex_login_found,
}


def provider_readiness(name: str, config: Config) -> ProviderStatus:
    """Local and read-only. A floor login CLI (unless provider_envs opts it into a
    key) needs to be on PATH with a stored login; everything else needs its key."""
    floor = BUILTIN_FLOOR.get(name)
    if floor is not None and floor.env_var is None and name not in config.provider_envs:
        if which(name) is None:
            return ProviderStatus(Readiness.CLI_NOT_FOUND, "CLI not found")
        if _LOGIN_FOUND[name](os.environ):
            return ProviderStatus(Readiness.SIGNED_IN, "signed in")
        return ProviderStatus(Readiness.NOT_SIGNED_IN, "not signed in")
    var = config.env_for(name)
    if os.environ.get(var):
        return ProviderStatus(Readiness.KEY_SET, f"{var} set")
    return ProviderStatus(Readiness.KEY_UNSET, f"{var} not set")


def _summary_hint(name: str, readiness: Readiness) -> str:
    if readiness is Readiness.NOT_SIGNED_IN:
        return f" · {BUILTIN_FLOOR[name].auth_hint}"
    if readiness is Readiness.CLI_NOT_FOUND:
        return f" · install it, then {BUILTIN_FLOOR[name].auth_hint}"
    return ""


def readiness_lines(names: Iterable[str], config: Config) -> list[str]:
    """`  <name> — <label><hint>` per floor provider that isn't ready; others get no line."""
    lines: list[str] = []
    for n in names:
        if n not in BUILTIN_FLOOR:
            continue
        status = provider_readiness(n, config)
        if not status.readiness.ready:
            lines.append(f"  {n} — {status.label}{_summary_hint(n, status.readiness)}")
    return lines


def selectable_providers() -> list[str]:
    """Floor entries a quest can be pinned to, in floor order: a full tier table
    is what keeps a pinned resolve from failing at the tier map."""
    return [n for n, e in BUILTIN_FLOOR.items() if e.tier_defaults is not None]


class ProviderGroups(NamedTuple):
    """selectable_providers() partitioned for display; every arming surface reads this."""
    configured: tuple[str, ...]
    also_installed: tuple[str, ...]
    needs_setup: tuple[str, ...]


def provider_groups(config: Config) -> ProviderGroups:
    """Configured in `providers:` order, the rest in floor order; env_check reads are local."""
    from imp.providers.resolve import provider_is_available

    selectable = selectable_providers()
    available = [n for n in selectable if provider_is_available(n)]
    configured = tuple(n for n in config.providers if n in available)
    return ProviderGroups(
        configured=configured,
        also_installed=tuple(n for n in available if n not in configured),
        needs_setup=tuple(n for n in selectable if n not in available),
    )


def provider_choice_error(name: str, config: Config) -> str | None:
    """None when NAME is selectable and available; otherwise the line to show the
    user. The surface adds its own prefix (`--provider: ` for the flag, none for
    the slash command)."""
    groups = provider_groups(config)
    if name in groups.configured or name in groups.also_installed:
        return None
    if name in groups.needs_setup:
        return f"{name} is not available — {BUILTIN_FLOOR[name].auth_hint}"
    segments = [f"{label}: {', '.join(names)}" for label, names in (
        ("configured", groups.configured),
        ("also installed", groups.also_installed),
        ("needs setup", groups.needs_setup),
    ) if names]
    return f"unknown provider {name!r} ({'; '.join(segments)})"


def _thunks(module: str) -> tuple[Callable[[], bool], Callable[[str], Provider]]:
    """Import-on-call thunks: env_check and factory look up module attributes per call."""
    def env_check() -> bool:
        mod = importlib.import_module(f"imp.providers.{module}")
        return cast(bool, mod._env_check())

    def factory(model: str) -> Provider:
        mod = importlib.import_module(f"imp.providers.{module}")
        return cast("Provider", mod._factory(model))

    return env_check, factory


def register_builtin_providers() -> None:
    """Register builtins from BUILTIN_FLOOR as import-on-call thunks."""
    from imp.providers.resolve import register_provider

    for name, entry in BUILTIN_FLOOR.items():
        env_check, factory = _thunks(entry.module)
        register_provider(
            name, env_check=env_check, factory=factory,
            tier_defaults=entry.tier_defaults, env_var=entry.env_var,
        )
