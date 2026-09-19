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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from imp.cli.accounting import (
    billing_from_agent_message,
    billing_from_usage_patch,
    model_rows,
)
from imp.providers.catalog import BillingUsage, cost_usd, prices_for

_CACHE_READ_FREE = frozenset({"claude", "anthropic"})
UNIT_EVENTS = ("query_result", "agent_message", "agent_message_usage_patch")


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


def _number(value: Any) -> float:
    """A JSON number, or 0.0 for anything else — a foreign log's field may be
    null, a string, or absent."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class Units:
    """Weighted units per provider. A zero draw is kept rather than dropped: it
    is what records that the provider did tracked work, which the per-gauge
    no-units state reads."""

    per_provider: tuple[tuple[str, float], ...] = ()

    @classmethod
    def of(cls, provider: str | None, amount: float) -> Units:
        return cls() if provider is None else cls(((provider, amount),))

    def get(self, provider: str) -> float:
        return next((a for p, a in self.per_provider if p == provider), 0.0)

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(p for p, _ in self.per_provider)

    def __add__(self, other: Units) -> Units:
        merged: dict[str, float] = {}
        for provider, amount in (*self.per_provider, *other.per_provider):
            merged[provider] = merged.get(provider, 0.0) + amount
        return Units(tuple(sorted(merged.items())))


NO_UNITS = Units()


@dataclass(frozen=True)
class Increment:
    """One request's units and the wall-clock interval it occupied, at `index` in
    the log it was read from.

    `start is None` is a point-in-time row — an `agent_message` or a codex usage
    patch, neither of which carries a duration. It belongs whole to the span
    holding `end` and has no interval to prorate over, so the overlap division
    below is reachable only where a real interval exists."""

    index: int
    agent_id: str
    session_id: str | None
    provider: str | None
    units: float
    start: datetime | None
    end: datetime

    def credit(self, start: datetime, end: datetime) -> float:
        """This increment's units falling inside `[start, end]`. An interval meeting
        the span in zero length carries none of them; an instant on the boundary
        belongs to the span, having no interval to divide."""
        if self.start is None:
            return self.units if start <= self.end <= end else 0.0
        overlap = (min(self.end, end) - max(self.start, start)).total_seconds()
        if overlap <= 0:
            return 0.0
        return self.units * overlap / (self.end - self.start).total_seconds()


def _delta_row(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    keys = set(current) | set(previous)
    return {k: max(0.0, _number(current.get(k)) - _number(previous.get(k))) for k in keys}


def _rollup_delta(current: dict[str, Any],
                  previous: dict[str, Any]) -> dict[str, Any]:
    """The per-`(model, bucket)` difference between two cumulative rollups. A
    model absent from the earlier one differences against zero; one absent from
    the later contributes nothing, since a cumulative bucket cannot shrink."""
    return {model: _delta_row(row, previous.get(model) or {})
            for model, row in current.items() if isinstance(row, dict)}


def increments(events: Iterable[dict[str, Any]],
               agents: dict[str, tuple[str | None, str | None, str | None]],
               ) -> list[Increment]:
    """Every usage-bearing row as one increment, in log order.

    `agents` both seeds and accumulates the per-agent
    `(provider, model, auth_mode)` triple: a usage patch and a `query_result`
    carry none of the three, and a neighbour's resolving `agent_message` may lie
    before the span, which is why `quota_neighbours` resolves it daemon-side.

    claude `query_result` rows are cumulative within an `(agent, session)` pair,
    so each differences against its predecessor in that pair and a new session
    restarts from zero. claude's own `agent_message` usage is the unreliable
    start-of-message snapshot `build_summary` also skips."""
    out: list[Increment] = []
    previous: dict[tuple[str, str], dict[str, Any]] = {}
    latest: datetime | None = None
    for index, e in enumerate(events):
        etype = e.get("type")
        when = parse_ts(e.get("timestamp"))
        if when is not None:
            latest = when
        elif etype == "agent_message_usage_patch":
            when = latest  # state.py writes the patch with three keys, no stamp
        if when is None or etype not in UNIT_EVENTS:
            continue
        agent_id = _text(e.get("agent_id"))
        p = e.get("payload") or {}
        if etype == "agent_message":
            agents[agent_id] = (p.get("provider"), p.get("model"),
                                p.get("auth_mode"))
            if p.get("provider") == "claude":
                continue
            billing = billing_from_agent_message(p)
            if billing is not None:
                out.append(Increment(
                    index=index, agent_id=agent_id, session_id=None,
                    provider=p.get("provider"),
                    units=weighted_units(p.get("provider"), p.get("model"),
                                         billing),
                    start=None, end=when))
        elif etype == "agent_message_usage_patch":
            billing = billing_from_usage_patch(p)
            if billing is not None:
                provider, model, _auth = agents.get(agent_id, (None, None, None))
                out.append(Increment(
                    index=index, agent_id=agent_id, session_id=None,
                    provider=provider,
                    units=weighted_units(provider, model, billing),
                    start=None, end=when))
        else:
            session_id = str(p.get("session_id") or "")
            pair = (agent_id, session_id)
            rollup = p.get("model_usage") or {}
            provider, _model, _auth = agents.get(agent_id, (None, None, None))
            units = rollup_units(provider,
                                 _rollup_delta(rollup, previous.get(pair, {})))
            previous[pair] = rollup
            duration_ms = _number(p.get("duration_ms"))
            out.append(Increment(
                index=index, agent_id=agent_id, session_id=session_id,
                provider=provider, units=units,
                start=(when - timedelta(milliseconds=duration_ms)
                       if duration_ms > 0 else None),
                end=when))
    return out


def units_in_span(rows: Iterable[Increment], start: datetime,
                  end: datetime) -> Units:
    """A quest's whole in-span draw: what a neighbour, which has no row structure
    to attribute against, contributes to the denominator."""
    total = NO_UNITS
    for inc in rows:
        total = total + Units.of(inc.provider, inc.credit(start, end))
    return total


def credits_by_index(events: list[dict[str, Any]],
                     span: tuple[datetime, datetime]) -> dict[int, Units]:
    """Each increment's in-span units, keyed by its position in `events` — the one
    join a row walk over the same list can rely on. An event id cannot serve: the
    persisted usage patch carries none, which is where a codex run's whole draw
    lives. One event yields at most one increment, so no key collides."""
    start, end = span
    return {inc.index: Units.of(inc.provider, inc.credit(start, end))
            for inc in increments(events, {})}
