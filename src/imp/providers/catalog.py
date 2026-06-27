"""Provider-side model catalog: the single source of model metadata.

Each provider owns its own alias→metadata table here (kept in the providers
layer, never the renderer — see docs/imp-invariants.md "Layering invariants").
`window` is a static fallback; for Claude Code the live SDK `rawMaxTokens`
supersedes it once a turn completes. `cli_model` overrides the alias passed to
the CLI/SDK when the alias is not a real model id (e.g. `opus-1m` →
`claude-opus-4-8[1m]`, the bracketed model-id that enables the 1M context
window under both subscription and API-key auth).
`betas` remains as a generic per-model opt-in field, currently unused.

Hand-maintained: no provider exposes context-window size via an endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_WINDOW = 200_000


@dataclass(frozen=True)
class ModelInfo:
    window: int = DEFAULT_WINDOW
    betas: tuple[str, ...] = ()
    cli_model: str | None = None


# NOTE on two window surfaces: the structured catalogs below carry the
# per-alias `window` (the spec mandates seeding ANTHROPIC/OPENAI even though
# only claude_code's _build_options reads a catalog table today); the renderer
# fallback uses the separate `_WINDOW_TOKENS` substring map further down. Keep
# the two in agreement when editing window values.
CLAUDE_CODE: dict[str, ModelInfo] = {
    "haiku": ModelInfo(window=200_000),
    "sonnet": ModelInfo(window=200_000),
    "opus": ModelInfo(window=200_000),
    # opus-1m requests the 1M window via the bracketed model-id form, which
    # both subscription (OAuth) and API-key auth honor. The earlier `betas`
    # opt-in only worked for API-key auth: under subscription auth claude-p
    # silently dropped the beta (window fell back to 200k) and warned to stderr
    # every request ("Custom betas are only available for API key users").
    "opus-1m": ModelInfo(
        window=1_000_000,
        cli_model="claude-opus-4-8[1m]",
    ),
}

ANTHROPIC: dict[str, ModelInfo] = {
    "haiku": ModelInfo(window=200_000),
    "sonnet": ModelInfo(window=200_000),
    "opus": ModelInfo(window=200_000),
}

OPENAI: dict[str, ModelInfo] = {
    "gpt-5": ModelInfo(window=400_000),
    "gpt-5.5": ModelInfo(window=400_000),
    "gpt-5.4-mini": ModelInfo(window=400_000),
    "gpt-5.3-codex": ModelInfo(window=400_000),
}

_DEFAULT = ModelInfo(window=DEFAULT_WINDOW)


def model_info(catalog: dict[str, ModelInfo], alias: str) -> ModelInfo:
    """Resolve an alias in a provider catalog, falling back to a conservative
    default (200k window, no betas, no cli_model override) for unknown aliases."""
    return catalog.get(alias, _DEFAULT)


# Provider-agnostic substring fallback for the renderer: an arbitrary model id
# string (alias or dated id) maps to a window. Ordered longest-token-first so
# "opus-1m" matches before "opus". This is the renderer's ONLY window source
# (the old _CONTEXT_WINDOWS dict that used to live in live_driver.py).
_WINDOW_TOKENS: tuple[tuple[str, int], ...] = (
    ("opus-1m", 1_000_000),
    ("haiku", 200_000),
    ("sonnet", 200_000),
    ("opus", 200_000),
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
    occupancy = (int(usage.get("input_tokens", 0) or 0)
                 + int(usage.get("cache_read_input_tokens", 0) or 0)
                 + int(usage.get("cache_creation_input_tokens", 0) or 0))
    return occupancy / window >= frac
