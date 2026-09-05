# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Provider-side model catalog: the single source of model metadata.

Each provider owns its own alias→metadata table here (kept in the providers
layer, never the renderer — see docs/internals/invariants.md "Layering invariants").
`window` is a static fallback; for Claude Code the live SDK `rawMaxTokens`
supersedes it once a turn completes. `cli_model` overrides the alias passed to
the CLI/SDK when the alias is not the wire model id (e.g. the raw Anthropic API
rejects the bracketed `claude-opus-4-8[1m]` form, so its entry sends
`claude-opus-4-8` and requests the 1M window via `betas`, the anthropic-beta
header opt-ins).

Hand-maintained: no provider exposes context-window size via an endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_WINDOW = 200_000
DEFAULT_MAX_OUTPUT_TOKENS = 32_000
DEFAULT_CHECKPOINT_FRACTION = 0.8


@dataclass(frozen=True)
class ModelInfo:
    window: int = DEFAULT_WINDOW
    betas: tuple[str, ...] = ()
    cli_model: str | None = None
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    supports_vision: bool = True
    # Absolute prompt-token handoff ceiling (a billing breakpoint), beside the
    # hard cap `window`. None -> the default window fraction applies.
    soft_cap: int | None = None


CLAUDE_CODE: dict[str, ModelInfo] = {
    # Undated major.minor ids used by imp.providers.BUILTIN_FLOOR; the bare aliases below
    # remain resolvable for operator config layers that still use them.
    "claude-haiku-4-5": ModelInfo(window=200_000),
    "claude-sonnet-4-6": ModelInfo(window=200_000),
    "claude-opus-4-8": ModelInfo(window=200_000),
    # The bracketed model-id form is a real CLI model id that requests the 1M
    # window under both subscription (OAuth) and API-key auth. A `betas` opt-in
    # would only work for API-key auth: under subscription auth claude-p
    # silently drops the beta (window falls back to 200k) and warns to stderr
    # every request ("Custom betas are only available for API key users").
    "claude-opus-4-8[1m]": ModelInfo(window=1_000_000),
    # Opus 5: window-only, mirroring the opus-4-8 entries. The bracketed form is
    # a real CLI model id passed through as-is (no cli_model/betas indirection).
    "claude-opus-5": ModelInfo(window=200_000),
    "claude-opus-5[1m]": ModelInfo(window=1_000_000),
    # Fable 5: window-only, mirroring the opus entries. The bracketed form is a
    # real CLI model id passed through as-is (no cli_model/betas indirection).
    "claude-fable-5": ModelInfo(window=200_000),
    "claude-fable-5[1m]": ModelInfo(window=1_000_000),
    "haiku": ModelInfo(window=200_000),
    "sonnet": ModelInfo(window=200_000),
    "opus": ModelInfo(window=200_000),
}

ANTHROPIC: dict[str, ModelInfo] = {
    # The raw API rejects the bracketed form, so cli_model strips it and the
    # context-1m beta header requests the 1M window instead (this provider is
    # API-key-only, where custom betas are honored — unlike claude_code above).
    "claude-opus-4-8[1m]": ModelInfo(
        window=1_000_000,
        max_output_tokens=64_000,
        cli_model="claude-opus-4-8",
        betas=("context-1m-2025-08-07",),
    ),
    # Opus 5: 128k output cap (2026-07-23 rollout doubles opus-4.8's 64k). The
    # [1m] form is rejected by the raw API, so cli_model strips it and the
    # context-1m beta header requests the 1M window (API-key-only provider).
    "claude-opus-5[1m]": ModelInfo(
        window=1_000_000,
        max_output_tokens=128_000,
        cli_model="claude-opus-5",
        betas=("context-1m-2025-08-07",),
    ),
    "claude-opus-5": ModelInfo(window=200_000, max_output_tokens=128_000),
    # Fable 5: 128k output cap confirmed live from /v1/models. The [1m] form is
    # rejected by the raw API, so cli_model strips it and the context-1m beta
    # header requests the 1M window (this provider is API-key-only).
    "claude-fable-5[1m]": ModelInfo(
        window=1_000_000,
        max_output_tokens=128_000,
        cli_model="claude-fable-5",
        betas=("context-1m-2025-08-07",),
    ),
    "claude-fable-5": ModelInfo(window=200_000, max_output_tokens=128_000),
    "claude-haiku-4-5": ModelInfo(window=200_000, max_output_tokens=32_000),
    "claude-sonnet-4-6": ModelInfo(window=200_000, max_output_tokens=32_000),
    "claude-opus-4-8": ModelInfo(window=200_000, max_output_tokens=64_000),
    "haiku": ModelInfo(window=200_000, max_output_tokens=32_000),
    "sonnet": ModelInfo(window=200_000, max_output_tokens=32_000),
    "opus": ModelInfo(window=200_000, max_output_tokens=64_000),
}

OPENAI: dict[str, ModelInfo] = {
    # 5.6 flavors (imp.providers.BUILTIN_FLOOR): one generation, three sizes —
    # luna < terra < sol. All four share a 1.05M window and a 128k output cap.
    # The bare `gpt-5.6` alias routes to Sol; it is here only so limits
    # resolution can cap it, and is hidden from the /model roster
    # (model_command._HIDDEN_ALIASES) because an operator writing it into a
    # `tiers:` block would get the flagship at every tier.
    # `soft_cap` is deliberate headroom UNDER the 272k billing breakpoint, not
    # the breakpoint itself: handing off at 260k keeps the whole request below
    # the point where it would bill at 2x input / 1.5x output.
    "gpt-5.6-sol": ModelInfo(
        window=1_050_000, max_output_tokens=128_000, soft_cap=260_000),
    "gpt-5.6-terra": ModelInfo(
        window=1_050_000, max_output_tokens=128_000, soft_cap=260_000),
    "gpt-5.6-luna": ModelInfo(
        window=1_050_000, max_output_tokens=128_000, soft_cap=260_000),
    "gpt-5.6": ModelInfo(
        window=1_050_000, max_output_tokens=128_000, soft_cap=260_000),
    "gpt-5": ModelInfo(window=400_000),
    # 5.5 and 5.4 share the 1.05M window, the 128k output cap and the same 272k
    # breakpoint as the 5.6 family, so they take the same handoff headroom.
    "gpt-5.5": ModelInfo(
        window=1_050_000, max_output_tokens=128_000, soft_cap=260_000),
    "gpt-5.4": ModelInfo(
        window=1_050_000, max_output_tokens=128_000, soft_cap=260_000),
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
    "claude_code": CLAUDE_CODE,
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


def checkpoint_cutoff(info: ModelInfo | None, window: int | None) -> float | None:
    """The handoff cutoff as a fraction of `window`; None without a usable window."""
    if not window or window <= 0:
        return None
    cap = info.soft_cap if info else None
    if cap is None:
        return DEFAULT_CHECKPOINT_FRACTION
    return min(DEFAULT_CHECKPOINT_FRACTION, cap / window)


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
    """True when a turn's prompt-token occupancy has reached the model's cutoff.

    Occupancy is `input + cache_read + cache_creation` (the prompt side of the
    window, matching the context gauge's `usage_prompt_tokens`), divided by the
    catalog window. Returns False when the provider/model pair is unknown or
    usage is absent — the watch never fires rather than guessing.
    """
    info = limits_for(provider, model)
    if info is None or not usage:
        return False
    frac = checkpoint_cutoff(info, info.window)
    if frac is None:
        return False
    return context_tokens_from_usage(usage) / info.window >= frac


def sdk_context_threshold_crossed(
    context_tokens: int | None, context_window: int | None,
    provider: str | None, model: str | None,
) -> bool:
    """True when a provider's own reported LIVE context occupancy has reached
    the checkpoint cutoff — claude_code's `get_context_usage()`, or codex's
    rollout `token_count` record. Unlike context_threshold_crossed (which sums
    prompt tokens against the static catalog window), this divides the
    provider's live context_tokens / context_window, and applies the model's
    soft cap to that window. Returns False whenever a live number is
    unavailable — the watch never fires rather than guessing.
    """
    frac = checkpoint_cutoff(limits_for(provider, model), context_window)
    if (frac is None or not context_window or context_window <= 0
            or not context_tokens or context_tokens <= 0):
        return False
    return context_tokens / context_window >= frac


LONG_CONTEXT_BREAKPOINT = 272_000
WEB_SEARCH_USD_PER_REQUEST = 0.01  # $10 per 1,000 requests


@dataclass(frozen=True)
class ModelPrices:
    """USD per million tokens, one rate per billing bucket.
    `long_context_threshold` is the prompt-token breakpoint above which the
    WHOLE request is surcharged; None means never surcharged."""
    input: float
    output: float
    cache_read: float
    cache_write: float
    long_context_threshold: int | None = None


# input, output, cache read, cache write — USD per million tokens.
MODEL_PRICES: dict[str, ModelPrices] = {
    # Anthropic: no long-context premium at any size — 4.6-and-later bill a 900k
    # request at the same per-token rate as a 9k one.
    "claude-opus-5": ModelPrices(5.00, 25.00, 0.50, 6.25),
    "claude-fable-5": ModelPrices(10.00, 50.00, 1.00, 12.50),
    "claude-opus-4-8": ModelPrices(5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-4-6": ModelPrices(3.00, 15.00, 0.30, 3.75),
    "claude-haiku-4-5": ModelPrices(1.00, 5.00, 0.10, 1.25),
    "opus": ModelPrices(5.00, 25.00, 0.50, 6.25),
    "sonnet": ModelPrices(3.00, 15.00, 0.30, 3.75),
    "haiku": ModelPrices(1.00, 5.00, 0.10, 1.25),
    # OpenAI: cache write is 1.25x input on the 5.6 family and the plain input
    # rate everywhere else. Sol's rate is promotional through 2026-11-21.
    "gpt-5.6-sol": ModelPrices(4.00, 20.00, 0.40, 5.00, LONG_CONTEXT_BREAKPOINT),
    "gpt-5.6-terra": ModelPrices(2.00, 12.00, 0.20, 2.50, LONG_CONTEXT_BREAKPOINT),
    "gpt-5.6-luna": ModelPrices(0.20, 1.20, 0.02, 0.25, LONG_CONTEXT_BREAKPOINT),
    "gpt-5.6": ModelPrices(4.00, 20.00, 0.40, 5.00, LONG_CONTEXT_BREAKPOINT),
    "gpt-5.5": ModelPrices(5.00, 30.00, 0.50, 5.00, LONG_CONTEXT_BREAKPOINT),
    "gpt-5.4": ModelPrices(2.50, 15.00, 0.25, 2.50, LONG_CONTEXT_BREAKPOINT),
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
    claude_code's flat `webSearchRequests`, and the raw API's nested
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
    over-reads by several multiples, so absent evidence must not become a 2x
    charge.
    """
    prices = prices_for(model)
    if prices is None:
        return None
    surcharged = (prices.long_context_threshold is not None
                  and prompt_tokens is not None
                  and prompt_tokens > prices.long_context_threshold)
    prompt_mult = 2.0 if surcharged else 1.0
    output_mult = 1.5 if surcharged else 1.0
    return ((usage.fresh_input * prices.input * prompt_mult
             + usage.cached_input * prices.cache_read * prompt_mult
             + usage.cache_write * prices.cache_write * prompt_mult
             + usage.output * prices.output * output_mult) / 1_000_000
            + usage.web_search_requests * WEB_SEARCH_USD_PER_REQUEST)
