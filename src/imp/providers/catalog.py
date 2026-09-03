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
    # `soft_cap` encodes the 272k billing breakpoint: a prompt above it bills
    # the WHOLE request at 2x input / 1.5x output.
    "gpt-5.6-sol": ModelInfo(
        window=1_050_000, max_output_tokens=128_000, soft_cap=260_000),
    "gpt-5.6-terra": ModelInfo(
        window=1_050_000, max_output_tokens=128_000, soft_cap=260_000),
    "gpt-5.6-luna": ModelInfo(
        window=1_050_000, max_output_tokens=128_000, soft_cap=260_000),
    "gpt-5.6": ModelInfo(
        window=1_050_000, max_output_tokens=128_000, soft_cap=260_000),
    "gpt-5": ModelInfo(window=400_000),
    "gpt-5.5": ModelInfo(window=400_000),
    "gpt-5.4": ModelInfo(window=400_000),
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
    """The model's view of prompt size for one turn: input + both cache buckets.

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
    """True when claude_code's SDK-reported LIVE context occupancy has reached
    the checkpoint cutoff. Unlike context_threshold_crossed (which sums prompt
    tokens against the static catalog window), this divides the SDK's own live
    context_tokens / context_window from get_context_usage(), and applies the
    model's soft cap to that window. Returns False whenever a live number is
    unavailable — the watch never fires rather than guessing.
    """
    frac = checkpoint_cutoff(limits_for(provider, model), context_window)
    if (frac is None or not context_window or context_window <= 0
            or not context_tokens or context_tokens <= 0):
        return False
    return context_tokens / context_window >= frac
