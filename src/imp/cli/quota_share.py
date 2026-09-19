# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Attributed quota share: what fraction of a weekly subscription window a run
consumed, charged to this quest rather than reported as the account gauge's raw
movement.

A request's draw on the gauge is its list-price cost with the cache-read bucket
free on the Anthropic family -- a free non-negative fit over 5,912 readings drove
that weight to 0.0000, and cache reads are 93% of all tokens -- and with every
bucket counted on OpenAI, which publishes a flat 25-credits-per-dollar
weighting. The long-prompt surcharge a request carries in dollars is no part of
that weighting, so nothing here hands `cost_usd` a prompt size."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from imp.cli.accounting import model_rows
from imp.providers.catalog import BillingUsage, cost_usd, prices_for

_CACHE_READ_FREE = frozenset({"claude", "anthropic"})


def parse_ts(value: Any) -> datetime | None:
    """A log's timestamp as a comparable instant; a naive one reads UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def run_span(events: Iterable[dict[str, Any]]) -> tuple[datetime, datetime] | None:
    """The run's wall clock: its first timestamped event to its last -- the widest
    interval the log evidences, needing no inference about which events bound the
    run. Not the active time the `time` column reports: the gauge keeps moving
    while a run is parked."""
    stamps = [when for when in (parse_ts(e.get("timestamp")) for e in events)
              if when is not None]
    if not stamps:
        return None
    return stamps[0], stamps[-1]


def weighted_units(provider: str | None, model: str | None,
                   billing: BillingUsage) -> float:
    """One request's draw on the quota gauge, in list-price dollars. 0.0 when the
    model is absent from the price table: the gauge moved by an unknowable
    amount, and a guessed rate is worse than no rate."""
    if provider not in _CACHE_READ_FREE:
        return cost_usd(model, billing, None) or 0.0
    prices = prices_for(model)
    if prices is None:
        return 0.0
    # cache_write is 2x input on every Anthropic key, so the table's own rate
    # carries the 2x weighting rather than a second literal to keep in sync.
    return (billing.fresh_input * prices.input
            + billing.cache_write * prices.cache_write
            + billing.output * prices.output) / 1_000_000


def rollup_units(provider: str | None, model_usage: dict[str, Any]) -> float:
    """A claude `query_result`'s `model_usage` rollup, priced per model. One
    blended rate over the summed buckets gives a different number the moment a
    turn's model mix changes."""
    return sum((weighted_units(provider, model, billing)
                for model, billing in model_rows(model_usage)), 0.0)
