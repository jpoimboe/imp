# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Provider-side model catalog: the single source of model metadata.

Each provider owns its own alias→metadata table here (kept in the providers
layer, never the renderer — see docs/internals/invariants.md "Layering invariants").
`window` is a static fallback; for Claude Code the live SDK `rawMaxTokens`
supersedes it once a turn completes. `cli_model` overrides the alias passed to
the CLI/SDK when the alias is not the wire model id (e.g. the raw Anthropic API
rejects the bracketed `claude-opus-4-8[1m]` form, so its entry sends
`claude-opus-4-8`). `betas` carries anthropic-beta header opt-ins.

Hand-maintained: no provider exposes context-window size via an endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_WINDOW = 200_000
DEFAULT_MAX_OUTPUT_TOKENS = 32_000
DEFAULT_CHECKPOINT_FRACTION = 0.8
# No model hands off later than this, however large its window.
DEFAULT_CHECKPOINT_CEILING = 200_000


@dataclass(frozen=True)
class ModelInfo:
    window: int = DEFAULT_WINDOW
    betas: tuple[str, ...] = ()
    cli_model: str | None = None
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    supports_vision: bool = True
    # Thinking can't be turned off through Claude Code: it rewrites `--thinking disabled`
    # to adaptive.
    adaptive_only: bool = False


CLAUDE: dict[str, ModelInfo] = {
    # Undated major.minor ids used by imp.providers.BUILTIN_FLOOR; the bare aliases below
    # remain resolvable for operator config layers that still use them.
    # Sonnet/Haiku 5.5: 1M is native; the bracketed sonnet form is a real CLI id kept
    # resolvable for configs that spell it. Haiku 5.5 has no [1m] form.
    "claude-haiku-5-5": ModelInfo(window=1_000_000, adaptive_only=True),
    "claude-sonnet-5-5": ModelInfo(window=1_000_000, adaptive_only=True),
    "claude-sonnet-5-5[1m]": ModelInfo(window=1_000_000, adaptive_only=True),
    "claude-haiku-4-5": ModelInfo(window=200_000),
    "claude-sonnet-4-6": ModelInfo(window=200_000),
    "claude-opus-4-8": ModelInfo(window=1_000_000),
    # These models serve 1M natively, so the bare id asks for nothing and the
    # bracketed form is just another real CLI model id, kept resolvable for
    # configs that spell it. No entry here carries a `betas` opt-in: it would
    # work only under API-key auth, and under subscription auth claude-p
    # silently drops it (window falls back to 200k) while warning to stderr
    # every request ("Custom betas are only available for API key users").
    "claude-opus-4-8[1m]": ModelInfo(window=1_000_000),
    # Opus 5.5: the 1M window is native, so neither form needs a beta opt-in.
    "claude-opus-5-5": ModelInfo(window=1_000_000, adaptive_only=True),
    "claude-opus-5-5[1m]": ModelInfo(window=1_000_000, adaptive_only=True),
    # Opus 5: window-only, mirroring the opus-4-8 entries. The bracketed form is
    # a real CLI model id passed through as-is (no cli_model/betas indirection).
    "claude-opus-5": ModelInfo(window=1_000_000),
    "claude-opus-5[1m]": ModelInfo(window=1_000_000),
    # Fable 5: window-only, mirroring the opus entries. The bracketed form is a
    # real CLI model id passed through as-is (no cli_model/betas indirection).
    "claude-fable-5": ModelInfo(window=1_000_000),
    "claude-fable-5[1m]": ModelInfo(window=1_000_000),
    # The bare aliases resolve to the 5.5 models.
    "haiku": ModelInfo(window=1_000_000, adaptive_only=True),
    "sonnet": ModelInfo(window=1_000_000, adaptive_only=True),
    "opus": ModelInfo(window=1_000_000, adaptive_only=True),
}

ANTHROPIC: dict[str, ModelInfo] = {
    # These models serve 1M natively, so the bare entries request nothing. The
    # raw API rejects the bracketed form, so cli_model strips it; its context-1m
    # beta is retained only so the [1m] alias keeps behaving as it always has
    # (this provider is API-key-only, where custom betas are honored — unlike
    # claude above).
    "claude-opus-4-8[1m]": ModelInfo(
        window=1_000_000,
        max_output_tokens=64_000,
        cli_model="claude-opus-4-8",
        betas=("context-1m-2025-08-07",),
    ),
    # Opus 5.5: 1M is native, so no context-1m beta on either form; cli_model
    # only strips the bracket the raw API rejects.
    "claude-opus-5-5": ModelInfo(
        window=1_000_000, max_output_tokens=128_000, adaptive_only=True),
    "claude-opus-5-5[1m]": ModelInfo(
        window=1_000_000,
        max_output_tokens=128_000,
        cli_model="claude-opus-5-5",
        adaptive_only=True,
    ),
    # Opus 5: 128k output cap (2026-07-23 rollout doubles opus-4.8's 64k). The
    # [1m] alias is handled as above: cli_model strips the bracket the raw API
    # rejects, and its context-1m beta is retained rather than load-bearing.
    "claude-opus-5[1m]": ModelInfo(
        window=1_000_000,
        max_output_tokens=128_000,
        cli_model="claude-opus-5",
        betas=("context-1m-2025-08-07",),
    ),
    "claude-opus-5": ModelInfo(window=1_000_000, max_output_tokens=128_000),
    # Fable 5: 128k output cap confirmed live from /v1/models. The [1m] alias is
    # handled as above: cli_model strips the bracket the raw API rejects, and its
    # context-1m beta is retained rather than load-bearing.
    "claude-fable-5[1m]": ModelInfo(
        window=1_000_000,
        max_output_tokens=128_000,
        cli_model="claude-fable-5",
        betas=("context-1m-2025-08-07",),
    ),
    "claude-fable-5": ModelInfo(window=1_000_000, max_output_tokens=128_000),
    # Sonnet/Haiku 5.5: 1M native, so the [1m] form only needs cli_model to strip the
    # bracket. Haiku 5.5 has no [1m] form.
    "claude-haiku-5-5": ModelInfo(
        window=1_000_000, max_output_tokens=128_000, adaptive_only=True),
    "claude-sonnet-5-5": ModelInfo(
        window=1_000_000, max_output_tokens=128_000, adaptive_only=True),
    "claude-sonnet-5-5[1m]": ModelInfo(
        window=1_000_000,
        max_output_tokens=128_000,
        cli_model="claude-sonnet-5-5",
        adaptive_only=True,
    ),
    "claude-haiku-4-5": ModelInfo(window=200_000, max_output_tokens=32_000),
    "claude-sonnet-4-6": ModelInfo(window=200_000, max_output_tokens=32_000),
    "claude-opus-4-8": ModelInfo(window=1_000_000, max_output_tokens=64_000),
    # The bare aliases resolve to the 5.5 models.
    "haiku": ModelInfo(
        window=1_000_000, max_output_tokens=128_000, adaptive_only=True),
    "sonnet": ModelInfo(
        window=1_000_000, max_output_tokens=128_000, adaptive_only=True),
    "opus": ModelInfo(
        window=1_000_000, max_output_tokens=128_000, adaptive_only=True),
}

OPENAI: dict[str, ModelInfo] = {
    "gpt-6-astra": ModelInfo(window=1_050_000, max_output_tokens=128_000),
    # 5.6 flavors: one generation, three sizes — luna < terra < sol.
    # All four share a 1.05M window and a 128k output cap.
    # The bare `gpt-5.6` alias routes to Sol; it is here only so limits
    # resolution can cap it, and is hidden from the /model roster
    # (model_command._HIDDEN_ALIASES) because an operator writing it into a
    # `tiers:` block would get the flagship at every tier.
    "gpt-5.6-sol": ModelInfo(window=1_050_000, max_output_tokens=128_000),
    "gpt-5.6-terra": ModelInfo(window=1_050_000, max_output_tokens=128_000),
    "gpt-5.6-luna": ModelInfo(window=1_050_000, max_output_tokens=128_000),
    "gpt-5.6": ModelInfo(window=1_050_000, max_output_tokens=128_000),
    "gpt-5": ModelInfo(window=400_000),
    # 5.5 and 5.4 share the 1.05M window and 128k output cap.
    "gpt-5.5": ModelInfo(window=1_050_000, max_output_tokens=128_000),
    "gpt-5.4": ModelInfo(window=1_050_000, max_output_tokens=128_000),
    "gpt-5.4-mini": ModelInfo(window=400_000),
    "gpt-5.3-codex": ModelInfo(window=400_000),
}

_DEFAULT = ModelInfo(window=DEFAULT_WINDOW)


def model_info(catalog: dict[str, ModelInfo], alias: str) -> ModelInfo:
    """Resolve an alias in a provider catalog, falling back to a conservative
    default (200k window, no betas, no cli_model override) for unknown aliases."""
    return catalog.get(alias, _DEFAULT)


# `codex` runs OpenAI models, so it shares OPENAI.
_CATALOGS: dict[str, dict[str, ModelInfo]] = {
    "anthropic": ANTHROPIC,
    "claude": CLAUDE,
    "openai": OPENAI,
    "codex": OPENAI,
}


def model_supports_vision(provider_name: str, model_name: str) -> bool:
    """True unless the model is flagged supports_vision=False; unknown provider/alias -> True."""
    catalog = _CATALOGS.get(provider_name)
    if catalog is None:
        return True
    return model_info(catalog, model_name).supports_vision


def catalog_aliases(provider_name: str) -> tuple[str, ...]:
    """Alias keys for a provider in table order; () for an unknown provider."""
    catalog = _CATALOGS.get(provider_name)
    return tuple(catalog) if catalog is not None else ()


def limits_for(provider: str | None, model: str | None) -> ModelInfo | None:
    """Exact alias, else the longest catalog key `model` starts with.

    None when the provider or model is unknown — callers never guess. Prefix,
    never substring: `"claude-opus-5".startswith("opus")` is false, so the bare
    aliases cannot false-hit a dated id, and `[1m]` keys match exactly.
    """
    if not provider or not model:
        return None
    catalog = _CATALOGS.get(provider)
    if catalog is None:
        return None
    exact = catalog.get(model)
    if exact is not None:
        return exact
    prefixes = [key for key in catalog if model.startswith(key)]
    if not prefixes:
        return None
    return catalog[max(prefixes, key=len)]


def handoff_limit(window: int | None) -> int | None:
    """Prompt-token occupancy at which a session hands off; None without a
    usable window."""
    if not window or window <= 0:
        return None
    return min(int(DEFAULT_CHECKPOINT_FRACTION * window), DEFAULT_CHECKPOINT_CEILING)


def context_tokens_from_usage(usage: dict[str, Any] | None) -> int:
    """One turn's BILLING prompt sum: input + both cache buckets.

    It equals live context occupancy only when the turn was a single model
    request — true on the anthropic/openai push paths, false on codex, where
    usage is summed over every request in the turn and runs several times
    higher than the prompt actually sent.

    Correct for both usage shapes: Anthropic's `input_tokens` EXCLUDES cached
    tokens (so the three-way sum is the true prompt), while OpenAI Responses
    carries no cache fields (so the sum reduces to `input_tokens`, which already
    includes cache). Missing fields count as 0.
    """
    if not usage:
        return 0
    return (int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("cache_read_input_tokens", 0) or 0)
            + int(usage.get("cache_creation_input_tokens", 0) or 0))


def context_threshold_crossed(
    usage: dict[str, Any] | None, provider: str | None, model: str | None,
) -> bool:
    """True when a turn's prompt-token occupancy has reached the model's handoff
    limit: 200k tokens, or 80% of the catalog window when that is smaller.

    Occupancy is `input + cache_read + cache_creation` (the prompt side of the
    window). Returns False when the provider/model pair is unknown or usage is
    absent — the watch never fires rather than guessing.
    """
    info = limits_for(provider, model)
    if info is None or not usage:
        return False
    limit = handoff_limit(info.window)
    if limit is None:
        return False
    return context_tokens_from_usage(usage) >= limit


def sdk_context_threshold_crossed(
    context_tokens: int | None, context_window: int | None,
) -> bool:
    """True when a provider's own reported LIVE context occupancy has reached the
    handoff limit — claude's `get_context_usage()`, or codex's rollout
    `token_count` record. Unlike context_threshold_crossed (which sums prompt
    tokens against the static catalog window), this compares the provider's live
    occupancy against `handoff_limit(context_window)`, the live window being
    authoritative. Returns False without a live positive pair.
    """
    limit = handoff_limit(context_window)
    if limit is None or not context_tokens or context_tokens <= 0:
        return False
    return context_tokens >= limit


WEB_SEARCH_USD_PER_REQUEST = 0.01  # $10 per 1,000 requests


@dataclass(frozen=True)
class LongContextSurcharge:
    """Above `threshold` prompt tokens the WHOLE request is repriced: every prompt
    bucket at `prompt_mult`, output at `output_mult`."""
    threshold: int
    prompt_mult: float
    output_mult: float


_OPENAI_LONG_CONTEXT = LongContextSurcharge(272_000, 2.0, 1.5)


@dataclass(frozen=True)
class ModelPrices:
    """USD per million tokens, one rate per billing bucket; `long_context` None
    means never surcharged."""
    input: float
    output: float
    cache_read: float
    cache_write: float
    long_context: LongContextSurcharge | None = None


# input, output, cache read, cache write — USD per million tokens.
MODEL_PRICES: dict[str, ModelPrices] = {
    # Anthropic: no long-context premium at any size — 4.6-and-later bill a 900k
    # request at the same per-token rate as a 9k one. Cache writes are 2x input:
    # the claude path writes 1-hour-TTL entries, not the 5-minute TTL's 1.25x.
    # Opus 5.5 reads cache at 0.05x input, not the family's usual 0.1x.
    "claude-opus-5-5": ModelPrices(4.00, 20.00, 0.20, 8.00),
    "claude-opus-5": ModelPrices(5.00, 25.00, 0.50, 10.00),
    "claude-fable-5": ModelPrices(10.00, 50.00, 1.00, 20.00),
    "claude-opus-4-8": ModelPrices(5.00, 25.00, 0.50, 10.00),
    "claude-sonnet-4-6": ModelPrices(3.00, 15.00, 0.30, 6.00),
    "claude-haiku-4-5": ModelPrices(1.00, 5.00, 0.10, 2.00),
    "opus": ModelPrices(5.00, 25.00, 0.50, 10.00),
    "sonnet": ModelPrices(3.00, 15.00, 0.30, 6.00),
    "haiku": ModelPrices(1.00, 5.00, 0.10, 2.00),
    # OpenAI: cache write is 1.25x input on Astra and the 5.6 family, and the
    # plain input rate everywhere else. Sol's rate is promotional through
    # 2026-11-21.
    "gpt-6-astra": ModelPrices(10.00, 50.00, 1.00, 12.50, _OPENAI_LONG_CONTEXT),
    "gpt-5.6-sol": ModelPrices(4.00, 20.00, 0.40, 5.00, _OPENAI_LONG_CONTEXT),
    "gpt-5.6-terra": ModelPrices(2.00, 12.00, 0.20, 2.50, _OPENAI_LONG_CONTEXT),
    "gpt-5.6-luna": ModelPrices(0.20, 1.20, 0.02, 0.25, _OPENAI_LONG_CONTEXT),
    "gpt-5.6": ModelPrices(4.00, 20.00, 0.40, 5.00, _OPENAI_LONG_CONTEXT),
    "gpt-5.5": ModelPrices(5.00, 30.00, 0.50, 5.00, _OPENAI_LONG_CONTEXT),
    "gpt-5.4": ModelPrices(2.50, 15.00, 0.25, 2.50, _OPENAI_LONG_CONTEXT),
    "gpt-5.4-mini": ModelPrices(0.75, 4.50, 0.075, 0.75),
    "gpt-5.3-codex": ModelPrices(1.75, 14.00, 0.175, 1.75),
    "gpt-5": ModelPrices(1.25, 10.00, 0.125, 1.25),
}


def prices_for(model: str | None) -> ModelPrices | None:
    """Strip any `[...]` suffix, then exact match, else the longest table key
    the id starts with. None when nothing matches — callers never guess a rate.

    Ids arrive bare (`claude-sonnet-4-6`), date-stamped
    (`claude-haiku-4-5-20251001`) and suffixed (`claude-opus-4-8[1m]`); an
    exact-match lookup would drop the latter two entirely.
    """
    if not model:
        return None
    base = model.split("[", 1)[0]
    exact = MODEL_PRICES.get(base)
    if exact is not None:
        return exact
    prefixes = [key for key in MODEL_PRICES if base.startswith(key)]
    if not prefixes:
        return None
    return MODEL_PRICES[max(prefixes, key=len)]


@dataclass(frozen=True)
class BillingUsage:
    """One request's canonical billing quadruple plus its web-search count.

    `fresh_input` is already net of whatever buckets its source path nests
    inside `input_tokens` — the per-provider normalizer owns that subtraction,
    and nothing here subtracts again.
    """
    fresh_input: int = 0
    cached_input: int = 0
    cache_write: int = 0
    output: int = 0
    web_search_requests: int = 0


def web_search_request_count(usage: dict[str, Any] | None) -> int:
    """Server-side web-search requests from either Anthropic-family carrier:
    claude's flat `webSearchRequests`, and the raw API's nested
    `server_tool_use.web_search_requests`."""
    if not usage:
        return 0
    flat = usage.get("webSearchRequests")
    if flat is not None:
        return int(flat)
    server_tool_use = usage.get("server_tool_use")
    if isinstance(server_tool_use, dict):
        return int(server_tool_use.get("web_search_requests", 0) or 0)
    return 0


def cost_usd(model: str | None, usage: BillingUsage,
             prompt_tokens: int | None = None) -> float | None:
    """List-price dollars for one request; None when `model` is absent from the
    price table, which is the only way a computed cost is unavailable.

    `prompt_tokens` is the caller's own occupancy reading for the long-context
    surcharge — the record's prompt sum on openai, `context_tokens` on a codex
    usage patch. None is never surcharged: the alternative codex signal
    over-reads by several multiples, so absent evidence must not become a
    surcharge.
    """
    prices = prices_for(model)
    if prices is None:
        return None
    prompt_mult = output_mult = 1.0
    surcharge = prices.long_context
    if (surcharge is not None and prompt_tokens is not None
            and prompt_tokens > surcharge.threshold):
        prompt_mult, output_mult = surcharge.prompt_mult, surcharge.output_mult
    return ((usage.fresh_input * prices.input * prompt_mult
             + usage.cached_input * prices.cache_read * prompt_mult
             + usage.cache_write * prices.cache_write * prompt_mult
             + usage.output * prices.output * output_mult) / 1_000_000
            + usage.web_search_requests * WEB_SEARCH_USD_PER_REQUEST)
