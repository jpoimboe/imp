"""Per-provider price table → dollars per cell (spec Cost & reproducibility §).
Prices are USD per million tokens. UPDATE these to current list prices before a
real sweep; an unknown (provider, model) RAISES so a sweep can never silently
misprice a cell. Wall-clock and tool-calls stay raw telemetry; dollars is the
comparison axis for Q3."""

from __future__ import annotations

from dataclasses import dataclass

from imp.evals.models import Telemetry


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_creation_per_mtok: float


# Placeholder list prices — VERIFY against current provider pricing before a sweep.
# Keyed (provider, model).
_PRICES: dict[tuple[str, str], Price] = {
    ("anthropic", "claude-sonnet-4-6"): Price(3.0, 15.0, 0.30, 3.75),
    ("anthropic", "claude-opus-4-7"): Price(15.0, 75.0, 1.50, 18.75),
    ("anthropic", "claude-haiku-4-5-20251001"): Price(1.0, 5.0, 0.10, 1.25),
    ("openai", "gpt-5"): Price(1.25, 10.0, 0.125, 1.25),
    ("openai", "gpt-5.2"): Price(1.25, 10.0, 0.125, 1.25),
    ("openai", "gpt-5.5"): Price(5.0, 30.0, 0.50, 5.0),
    ("openai", "gpt-5.3-codex"): Price(1.75, 14.0, 0.175, 1.75),
    ("openai", "gpt-5.4-mini"): Price(0.75, 4.50, 0.075, 0.75),
    ("openai", "gpt-5-mini"): Price(0.25, 2.0, 0.025, 0.25),
    # Judge: priced only in the dry-run estimate (the run path never prices the
    # judge). Placeholder = the opus-4-7 tuple; swap in real opus-4-8 list prices
    # if you have them to hand. Cost is not a decision driver — this only sharpens
    # the `dry-run` estimate.
    ("anthropic", "claude-opus-4-8"): Price(15.0, 75.0, 1.50, 18.75),
}


def price_for(provider: str, model: str) -> Price:
    return _PRICES[(provider, model)]


def dollars_for(telemetry: Telemetry, provider: str, model: str) -> float:
    p = price_for(provider, model)
    return (
        telemetry.input_tokens * p.input_per_mtok
        + telemetry.output_tokens * p.output_per_mtok
        + telemetry.cache_read_tokens * p.cache_read_per_mtok
        + telemetry.cache_creation_tokens * p.cache_creation_per_mtok
    ) / 1_000_000
