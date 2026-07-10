"""Provider-side model catalog: the single source of model metadata.

Each provider owns its own alias→metadata table here (kept in the providers
layer, never the renderer — see docs/imp-invariants.md "Layering invariants").
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


@dataclass(frozen=True)
class ModelInfo:
    window: int = DEFAULT_WINDOW
    betas: tuple[str, ...] = ()
    cli_model: str | None = None
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    supports_vision: bool = True


# NOTE on two window surfaces: the structured catalogs below carry the
# per-alias `window` (the spec mandates seeding ANTHROPIC/OPENAI even though
# only claude_code's _build_options reads a catalog table today); the renderer
# fallback uses the separate `_WINDOW_TOKENS` substring map further down. Keep
# the two in agreement when editing window values.
CLAUDE_CODE: dict[str, ModelInfo] = {
    # Undated major.minor ids used by TIER_DEFAULTS; the bare aliases below
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


# Provider-agnostic substring fallback for the renderer: an arbitrary model id
# string (alias or dated id) maps to a window. Ordered so the "[1m]" marker
# wins over the base-model tokens it always accompanies. This is the renderer's
# ONLY window source (the old _CONTEXT_WINDOWS dict from live_driver.py).
_WINDOW_TOKENS: tuple[tuple[str, int], ...] = (
    ("[1m]", 1_000_000),
    ("haiku", 200_000),
    ("sonnet", 200_000),
    ("opus", 200_000),
    ("fable", 200_000),
    ("gpt-5", 400_000),
)


def window_for_model(model: str | None) -> int | None:
    if not model:
        return None
    lowered = model.lower()
    for token, size in _WINDOW_TOKENS:
        if token in lowered:
            return size
    return None


def checkpoint_fraction(
    threshold: float | int | None, window: int | None
) -> float | None:
    """The context-checkpoint cutoff as a fraction of the model window (None if unresolvable)."""
    if threshold is None or threshold <= 0:
        return None
    if threshold <= 1:
        return float(threshold)
    if window is None or window <= 0:
        return None
    return threshold / window


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
    usage: dict[str, Any] | None, model: str | None,
    threshold: float | int | None,
) -> bool:
    """True when a turn's prompt-token occupancy has reached the checkpoint cutoff.

    Occupancy is `input + cache_read + cache_creation` (the prompt side of the
    window, matching the context gauge's `usage_prompt_tokens`). The cutoff is
    `checkpoint_fraction(threshold, window)` of the model window. Returns False
    whenever the threshold, window, or usage is unavailable — the watch simply
    never fires rather than guessing.
    """
    window = window_for_model(model)
    frac = checkpoint_fraction(threshold, window)
    if frac is None or window is None or window <= 0 or not usage:
        return False
    occupancy = context_tokens_from_usage(usage)
    return occupancy / window >= frac


def sdk_context_threshold_crossed(
    context_tokens: int | None, context_window: int | None,
    threshold: float | int | None,
) -> bool:
    """True when claude_code's SDK-reported LIVE context occupancy has reached
    the checkpoint cutoff. Unlike context_threshold_crossed (which sums prompt
    tokens against the static catalog window), this divides the SDK's own live
    context_tokens / context_window from get_context_usage(). Returns False
    whenever any input is unavailable — the watch never fires rather than
    guessing.
    """
    frac = checkpoint_fraction(threshold, context_window)
    if (frac is None or not context_window or context_window <= 0
            or not context_tokens or context_tokens <= 0):
        return False
    return context_tokens / context_window >= frac
