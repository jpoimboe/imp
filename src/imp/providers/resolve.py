"""Resolve a RoleConfig to a Provider by walking its prefer: list (spec §6.2).

First entry whose env_check returns True wins. Mid-run failover is out of
V1 scope; resolution happens once at startup.

Providers register via providers.anthropic / providers.openai / providers.claude_code
on module import. Tests use register_provider directly with fakes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from imp.engine.errors import LoadError
from imp.engine.model_rules import find_constraint_cycle
from imp.engine.model_tiers import effective_model_tiers
from imp.providers.base import Provider
from imp.providers.config import ProviderModel, RoleConfig

if TYPE_CHECKING:
    from imp.engine.types import RoleConstraint, Workflow
    from imp.providers.config import Config

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


def provider_from_model(pm: ProviderModel) -> Provider:
    """Rebuild a live Provider from a persisted provider+model pair. Used by the
    executor (per run-entry) and the subagent spawn helper. env_check is NOT
    consulted — the provider was already chosen at resolution time."""
    if pm.provider not in _PROVIDERS:
        raise UnknownProvider(
            f"unknown provider {pm.provider!r} (known: {sorted(_PROVIDERS)})"
        )
    _env_check, factory = _PROVIDERS[pm.provider]
    return factory(pm.model)


async def spawn_subagent_via_registry(sub_invocation: Any) -> Any:
    """Provider-agnostic subagent spawn: build the child RUNNER from
    sub_invocation.provider_name via the registry, so a subagent can run on a
    DIFFERENT vendor than its parent. Returns the runner (NOT runner.run()), so
    dispatch_task can pass it to drive_invocation — which attaches the
    subagent runner's interrupt to the coordinator (clean ESC mid-subagent) and
    drives it with the shared error_policy/nudge/pause loop."""
    provider = provider_from_model(
        ProviderModel(
            provider=sub_invocation.provider_name,
            model=sub_invocation.model_name,
        )
    )
    return provider.create_runner(sub_invocation)


def _differ_tier(entry: ProviderModel, tgt_provider: str, tgt_model: str) -> int:
    """Score one prefer-list candidate against the differ-target's resolution.
    1=different vendor (best), 2=same vendor different model, 3=identical (worst)."""
    if entry.provider != tgt_provider:
        return 1
    if entry.model != tgt_model:
        return 2
    return 3


def _topo_order(roles: set[str], active: dict[str, RoleConstraint]) -> list[str]:
    """Order roles so each differ-target precedes its dependent. Assumes no
    cycle (the caller checks first). Deterministic: roots visited sorted."""
    order: list[str] = []
    seen: set[str] = set()

    def visit(r: str) -> None:
        if r in seen:
            return
        c = active.get(r)
        if c is not None and c.differ_provider_from is not None:
            visit(c.differ_provider_from)
        seen.add(r)
        order.append(r)

    for r in sorted(roles):
        visit(r)
    return order


def resolve_roles(
    workflow: Workflow,
    config: Config,
) -> tuple[dict[str, ProviderModel], dict[str, dict[str, Any]]]:
    """Global, once-per-run role resolution (design "Resolution algorithm").

    Returns (resolved, details):
      resolved[role] = ProviderModel(provider, model)         -- persisted in State
      details[role]  = {"provider", "model", "constraint": str|None,
                        "differ_tier": int|None,
                        "differ_from": str|None}              -- for roles_resolved

    This is the authoritative validator: it runs inside run_change where the
    change-creation validators do not, so it owns the unconfigured-role and
    cycle checks even though those are ALSO flagged earlier as a convenience.
    """
    # 1. Collect roles: every agent step's model_role + the model_role of every
    #    subagent reachable one level deep (dispatch_task gives children no
    #    subagents of their own, so one level is exhaustive).
    roles: set[str] = set()
    for step in workflow.flat_steps():
        if step.kind != "agent" or step.file is None:
            continue
        roles.add(step.model_role)
        agent = workflow.agents.get(step.file.stem)
        if agent is None:
            continue
        for sub_name in agent.subagents:
            sub = workflow.agents.get(sub_name)
            if sub is not None and sub.model_role is not None:
                roles.add(sub.model_role)

    # Tiers are roles: every effective model_tiers entry must resolve + persist,
    # so a runtime model_tier choice finds its provider/model in the map. This
    # also makes step 4's unconfigured-role LoadError the authoritative backstop
    # for tier roles (validators.py only flags them at change-creation time).
    for tier in effective_model_tiers(workflow, config):
        roles.add(tier)

    # 2. Merge constraints: workflow first, config overrides per role.
    merged: dict[str, RoleConstraint] = dict(workflow.model_rules)
    merged.update(config.model_rules)

    # Pull in differ-targets of any collected, constrained role (fixpoint — a
    # target may itself be constrained).
    changed = True
    while changed:
        changed = False
        for role in list(roles):
            c = merged.get(role)
            if (
                c is not None
                and c.differ_provider_from is not None
                and c.differ_provider_from not in roles
            ):
                roles.add(c.differ_provider_from)
                changed = True

    active = {r: merged[r] for r in roles if r in merged}

    # 3. Cycle / self-reference check on the MERGED graph (authoritative).
    cycle = find_constraint_cycle(active)
    if cycle is not None:
        raise LoadError("model_rules constraint cycle: " + " -> ".join(cycle))

    # 4. Resolve in topological order (targets before dependents).
    resolved: dict[str, ProviderModel] = {}
    details: dict[str, dict[str, Any]] = {}
    for role in _topo_order(roles, active):
        if role not in config.roles:
            raise LoadError(
                f"model_role {role!r} is not configured in imp/config.yaml"
            )
        role_cfg = config.roles[role]
        constraint = active.get(role)

        if constraint is None or constraint.differ_provider_from is None:
            prov = resolve_role(role_cfg)  # env-first; byte-for-byte today's path
            pm = ProviderModel(provider=prov.name, model=prov.model)
            resolved[role] = pm
            details[role] = {
                "provider": pm.provider,
                "model": pm.model,
                "constraint": None,
                "differ_tier": None,
                "differ_from": None,
            }
            continue

        target = constraint.differ_provider_from
        tpm = resolved[target]  # topo order guarantees the target is resolved
        available: list[tuple[int, ProviderModel]] = []
        for i, entry in enumerate(role_cfg.prefer):
            if entry.provider not in _PROVIDERS:
                raise UnknownProvider(
                    f"role {role!r}: unknown provider {entry.provider!r} "
                    f"(known: {sorted(_PROVIDERS)})"
                )
            env_check, _factory = _PROVIDERS[entry.provider]
            if env_check():
                available.append((i, entry))
        if not available:
            raise ProviderUnavailable(
                f"role {role!r}: no provider available after env-filter"
            )

        chosen = min(
            available,
            key=lambda ie: (_differ_tier(ie[1], tpm.provider, tpm.model), ie[0]),
        )[1]
        chosen_tier = _differ_tier(chosen, tpm.provider, tpm.model)
        pm = ProviderModel(provider=chosen.provider, model=chosen.model)
        resolved[role] = pm
        details[role] = {
            "provider": pm.provider,
            "model": pm.model,
            "constraint": "differ_provider_from",
            "differ_tier": chosen_tier,
            "differ_from": target,
        }

    return resolved, details
