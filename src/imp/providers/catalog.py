"""Provider-side model catalog: the single source of model metadata.

Each provider owns its own alias→metadata table here (kept in the providers
layer, never the renderer — see docs/imp-invariants.md "Layering invariants").
`window` is a static fallback; for Claude Code the live SDK `rawMaxTokens`
supersedes it once a turn completes. `betas` is load-bearing — it is what
enables the 1M context window. `cli_model` overrides the alias passed to the
CLI/SDK when the alias is not a real model id (e.g. `opus-1m` → `opus`).

Hand-maintained: no provider exposes context-window size via an endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass

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
    # opus-1m runs the `opus` CLI model with the 1M beta requested. If a live
    # run reads back rawMaxTokens == 200_000, switch cli_model to the model-id
    # form "claude-opus-4-8[1m]" (see the live-verification task).
    "opus-1m": ModelInfo(
        window=1_000_000,
        betas=("context-1m-2025-08-07",),
        cli_model="opus",
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
    "gpt-5.4": ModelInfo(window=400_000),
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
