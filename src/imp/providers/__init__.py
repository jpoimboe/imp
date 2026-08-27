# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Josh Poimboeuf
"""V1 provider registry. Importing this package registers NOTHING: the builtins
land in providers.resolve._PROVIDERS on the first registry read, via
resolve._ensure_builtins(). Registration binds lazy thunks over BUILTIN_FLOOR, so
no registry read imports a provider module — not `imp --help`, not a dispatching
command's effective-config read — and with them no httpx (and no
rich/click/pygments behind httpx's CLI extra). A provider module loads only when
its env_check or factory is actually called.

Registration is idempotent — re-running it is a no-op for already-registered
names (resolve.register_provider rebinds, which is harmless in practice, but
tests that need isolation should call resolve.reset_providers_for_tests()).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, NamedTuple, cast

if TYPE_CHECKING:
    from collections.abc import Callable

    from imp.providers.base import Provider


class BuiltinFloor(NamedTuple):
    """One builtin's registration data: module name plus floor metadata for register_provider."""
    module: str
    tier_defaults: dict[str, str] | None
    env_var: str | None


# Pinned undated major.minor ids: they track dated snapshots of that minor but never
# jump to a new major/minor release — a bare `sonnet`/`gpt-5` alias would silently
# upgrade the shipped floor. codex refuses *-codex models (runner preflight M1).
BUILTIN_FLOOR: dict[str, BuiltinFloor] = {
    "anthropic": BuiltinFloor(
        module="anthropic",
        tier_defaults={
            "low": "claude-haiku-4-5",
            "medium": "claude-sonnet-4-6",
            "high": "claude-opus-5[1m]",
        },
        env_var="ANTHROPIC_API_KEY",
    ),
    "openai": BuiltinFloor(
        module="openai",
        tier_defaults={"low": "gpt-5.4-mini", "medium": "gpt-5.4", "high": "gpt-5.5"},
        env_var="OPENAI_API_KEY",
    ),
    "claude_code": BuiltinFloor(
        module="claude_code",
        tier_defaults={
            "low": "claude-haiku-4-5",
            "medium": "claude-sonnet-4-6",
            "high": "claude-opus-5[1m]",
        },
        env_var=None,  # CLI-authenticated (claude login); no env var
    ),
    "codex": BuiltinFloor(
        module="codex",
        tier_defaults={"low": "gpt-5.4-mini", "medium": "gpt-5.4", "high": "gpt-5.5"},
        env_var=None,  # CLI-authenticated (codex login); no env var
    ),
    # Duplicates scripted.ENV_VAR (read at runtime there); a test pins the two equal.
    "scripted": BuiltinFloor(
        module="scripted", tier_defaults=None, env_var="IMP_SCRIPTED_SCRIPT",
    ),
}


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
