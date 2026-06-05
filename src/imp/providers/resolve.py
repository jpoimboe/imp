"""Two-pass per-run resolution (spec §6 + Decisions).

Pass 1 — Tier map: for each configured tier name, walk `config.providers` in
order; first env-available vendor wins; look up `tiers[tier][vendor]` → model.
Result: `resolved_tiers: dict[tier_name, ProviderModel]`. Used as the
subagent-dispatch menu AND as the default for each step.

Pass 2 — Per-step overrides: for any agent step that declares `differ_from:`,
walk `config.providers` again scoring each available vendor against the
target step's resolved (provider, model) with `_differ_tier` (1=different
vendor, 2=same vendor different model, 3=identical). The lowest-scoring
available vendor wins at the step's tier. Result: `resolved_steps:
dict[step_name, ProviderModel]`. Topological order over the differ_from DAG
guarantees each target resolves before its dependents.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from imp.engine.differ_graph import find_differ_from_cycle, topo_order
from imp.engine.errors import LoadError
from imp.providers.base import Provider
from imp.providers.config import ProviderModel

if TYPE_CHECKING:
    from imp.engine.types import Workflow
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


def provider_from_model(pm: ProviderModel) -> Provider:
    """Rebuild a live Provider from a persisted provider+model pair. env_check
    is NOT consulted — the provider was already chosen at resolution time."""
    if pm.provider not in _PROVIDERS:
        raise UnknownProvider(
            f"unknown provider {pm.provider!r} (known: {sorted(_PROVIDERS)})"
        )
    _env_check, factory = _PROVIDERS[pm.provider]
    return factory(pm.model)


async def spawn_subagent_via_registry(sub_invocation: Any) -> Any:
    """Provider-agnostic subagent spawn: build the child RUNNER from
    sub_invocation.provider_name via the registry, so a subagent can run on a
    DIFFERENT vendor than its parent."""
    provider = provider_from_model(
        ProviderModel(
            provider=sub_invocation.provider_name,
            model=sub_invocation.model_name,
        )
    )
    return provider.create_runner(sub_invocation)


def _first_available_provider(config: Config) -> str | None:
    """Walk config.providers in order; return the first whose env_check passes.
    None means no provider is available at all (caller errors)."""
    for name in config.providers:
        if name not in _PROVIDERS:
            raise UnknownProvider(
                f"provider {name!r} is named in config.yaml providers: but is "
                f"not registered (known: {sorted(_PROVIDERS)})"
            )
        env_check, _ = _PROVIDERS[name]
        if env_check():
            return name
    return None


def _differ_tier(candidate_provider: str, candidate_model: str,
                 tgt_provider: str, tgt_model: str) -> int:
    """1 = different vendor (best), 2 = same vendor different model,
    3 = identical (worst). Scores a candidate against a target's resolution."""
    if candidate_provider != tgt_provider:
        return 1
    if candidate_model != tgt_model:
        return 2
    return 3


def resolve_run(
    workflow: Workflow, config: Config,
) -> tuple[
    dict[str, ProviderModel],          # resolved_tiers
    dict[str, ProviderModel],          # resolved_steps (only diverged steps)
    dict[str, dict[str, Any]],         # details: {"tiers": {...}, "steps": {...}}
]:
    """Two-pass resolution. Raises LoadError on unconfigured tier, missing
    differ_from target, or differ_from cycle. Raises ProviderUnavailable if
    no env-available vendor exists for a required tier."""
    # — Pass 1: tier map ————————————————————————————————————————————————————
    resolved_tiers: dict[str, ProviderModel] = {}
    tier_details: dict[str, dict[str, Any]] = {}
    for tier_name in config.tiers:
        winner = _first_available_provider(config)
        if winner is None:
            raise ProviderUnavailable(
                f"tier {tier_name!r}: no provider available (tried "
                f"{list(config.providers)})"
            )
        if winner not in config.tiers[tier_name]:
            raise LoadError(
                f"tier {tier_name!r} has no model configured for "
                f"provider {winner!r}",
            )
        model = config.tiers[tier_name][winner]
        resolved_tiers[tier_name] = ProviderModel(provider=winner, model=model)
        tier_details[tier_name] = {
            "provider": winner, "model": model,
            "differ_from": None, "differ_tier": None,
        }

    # — Pass 2: per-step overrides for differ_from ————————————————————————
    flat = workflow.flat_steps()
    agent_step_names = {s.name for s in flat if s.kind == "agent"}
    edges: dict[str, str] = {}
    for step in flat:
        if step.kind != "agent" or step.differ_from is None:
            continue
        if step.differ_from == step.name:
            raise LoadError(
                f"differ_from on step {step.name!r} cannot reference itself"
            )
        if step.differ_from not in agent_step_names:
            raise LoadError(
                f"differ_from target {step.differ_from!r} (on step "
                f"{step.name!r}) does not name an agent step"
            )
        if step.tier not in resolved_tiers:
            raise LoadError(
                f"step {step.name!r}: tier {step.tier!r} is not configured"
            )
        edges[step.name] = step.differ_from

    cyc = find_differ_from_cycle(edges)
    if cyc is not None:
        raise LoadError("differ_from cycle: " + " -> ".join(cyc))

    resolved_steps: dict[str, ProviderModel] = {}
    step_details: dict[str, dict[str, Any]] = {}
    step_by_name = {s.name: s for s in flat}
    # topo_order needs all steps involved in edges (both keys and values)
    all_steps_with_edges = set(edges.keys()) | set(edges.values())
    for step_name in topo_order(list(all_steps_with_edges), edges):
        if step_name not in edges:
            continue
        cur_step = step_by_name[step_name]
        target_name = edges[step_name]
        target_step = step_by_name.get(target_name)
        # Target's resolved (provider, model): per-step override if it has its
        # own differ_from override, else the target's tier resolution.
        if target_name in resolved_steps:
            tgt = resolved_steps[target_name]
        elif target_step is not None:
            tgt = resolved_tiers[target_step.tier]
        else:
            # Validator catches this earlier; defensive only.
            raise LoadError(f"differ_from target {target_name!r} not resolvable")

        # Walk config.providers; score each AVAILABLE vendor at this step's
        # tier; pick the lowest score, breaking ties by config order.
        best_score: int | None = None
        best_provider: str | None = None
        best_i: int = 0
        for i, prov_name in enumerate(config.providers):
            if prov_name not in _PROVIDERS:
                raise UnknownProvider(
                    f"provider {prov_name!r} is named in config.yaml "
                    f"providers: but is not registered "
                    f"(known: {sorted(_PROVIDERS)})"
                )
            env_check, _ = _PROVIDERS[prov_name]
            if not env_check():
                continue
            if prov_name not in config.tiers[cur_step.tier]:
                continue   # this vendor has no model for this tier
            cand_model = config.tiers[cur_step.tier][prov_name]
            score = _differ_tier(prov_name, cand_model, tgt.provider, tgt.model)
            if best_score is None or (score, i) < (best_score, best_i):
                best_score = score
                best_provider = prov_name
                best_i = i
        if best_provider is None:
            raise ProviderUnavailable(
                f"step {step_name!r}: no provider available at tier "
                f"{cur_step.tier!r}"
            )
        model = config.tiers[cur_step.tier][best_provider]
        resolved_steps[step_name] = ProviderModel(
            provider=best_provider, model=model)
        step_details[step_name] = {
            "provider": best_provider, "model": model,
            "differ_from": target_name, "differ_tier": best_score,
        }

    details = {"tiers": tier_details, "steps": step_details}
    return resolved_tiers, resolved_steps, details
