"""Resolve a RoleConfig to a langchain BaseChatModel by walking its prefer: list.

The first entry whose provider is supported AND whose env var is set wins.
Resolution happens once per run (at engine startup); mid-run failover is
explicitly out of V0 scope (spec §10).
"""

from __future__ import annotations

import os
from collections.abc import Callable

from langchain_core.language_models import BaseChatModel

from imp.providers.config import RoleConfig

# provider name → (env var, factory). Task 7 adds OpenAI here.
_PROVIDERS: dict[str, tuple[str, Callable[[str], BaseChatModel]]] = {}


def _register(name: str, env_var: str, factory: Callable[[str], BaseChatModel]) -> None:
    _PROVIDERS[name] = (env_var, factory)


def _anthropic_factory(model: str) -> BaseChatModel:
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=model)  # type: ignore[call-arg]


_register("anthropic", "ANTHROPIC_API_KEY", _anthropic_factory)


def _openai_factory(model: str) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=model)  # type: ignore[call-arg]


_register("openai", "OPENAI_API_KEY", _openai_factory)


class UnknownProvider(Exception):
    """prefer: lists a provider name we don't know how to instantiate."""


class ProviderUnavailable(Exception):
    """Every entry's env var was missing."""


def resolve_role(role: RoleConfig) -> BaseChatModel:
    """Walk role.prefer in order; return the first available BaseChatModel.

    Raises UnknownProvider on the first entry whose provider isn't registered
    (configuration error, not transient).
    Raises ProviderUnavailable only if every entry's env var is missing.
    """
    missing: list[str] = []
    for entry in role.prefer:
        if entry.provider not in _PROVIDERS:
            raise UnknownProvider(
                f"role {role.name!r}: unknown provider {entry.provider!r} "
                f"(known: {sorted(_PROVIDERS)})"
            )
        env_var, factory = _PROVIDERS[entry.provider]
        if not os.environ.get(env_var):
            missing.append(f"{entry.provider}({env_var})")
            continue
        return factory(entry.model)

    raise ProviderUnavailable(
        f"role {role.name!r}: no provider available; "
        f"tried {', '.join(missing) if missing else 'nothing'}"
    )
