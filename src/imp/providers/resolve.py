"""Resolve a RoleConfig to a Provider by walking its prefer: list (spec §6.2).

First entry whose env_check returns True wins. Mid-run failover is out of
V1 scope; resolution happens once at startup.

Providers register via providers.anthropic / providers.openai / providers.claude_code
on module import. Tests use register_provider directly with fakes.
"""

from __future__ import annotations

from collections.abc import Callable

from imp.providers.base import Provider
from imp.providers.config import RoleConfig

_PROVIDERS: dict[str, tuple[Callable[[], bool], Callable[[str], Provider]]] = {}


class UnknownProvider(Exception):
    pass


class ProviderUnavailable(Exception):
    pass


def register_provider(
    name: str,
    env_check: Callable[[], bool],
    factory: Callable[[str], Provider],
) -> None:
    _PROVIDERS[name] = (env_check, factory)


def reset_providers_for_tests() -> None:
    _PROVIDERS.clear()


def resolve_role(role: RoleConfig) -> Provider:
    missing: list[str] = []
    for entry in role.prefer:
        if entry.provider not in _PROVIDERS:
            raise UnknownProvider(
                f"role {role.name!r}: unknown provider {entry.provider!r} "
                f"(known: {sorted(_PROVIDERS)})"
            )
        env_check, factory = _PROVIDERS[entry.provider]
        if not env_check():
            missing.append(entry.provider)
            continue
        return factory(entry.model)
    raise ProviderUnavailable(
        f"role {role.name!r}: no provider available; tried {', '.join(missing) or 'nothing'}"
    )
