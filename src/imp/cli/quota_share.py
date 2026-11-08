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

from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from imp.cli.accounting import (
    billing_from_agent_message,
    billing_from_usage_patch,
    is_metered,
    model_rows,
)
from imp.cli.store import GaugeWeek
from imp.providers.catalog import BillingUsage, cost_usd, prices_for

_CACHE_READ_FREE = frozenset({"claude", "anthropic"})
UNIT_EVENTS = ("query_result", "agent_message", "agent_message_usage_patch")
_WEEKLY = "7d"
# The provider quantizes utilization to 0.01 -- one point of the weekly window.
_GAUGE_QUANTUM = 1.0
_WEEK = timedelta(days=7)


def parse_ts(value: Any) -> datetime | None:
    """A log's timestamp as a comparable instant; a naive one reads UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


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


def _number(value: Any) -> float:
    """A JSON number, or 0.0 for anything else — a foreign log's field may be
    null, a string, or absent."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class Increment:
    """One request's raw billing and the wall-clock interval it occupied, at
    `index` in the log it was read from. Prices and metering are applied on
    read, so the facts stay valid when either changes. `start is None` is a
    point-in-time row — an `agent_message` or a codex usage patch, neither of
    which carries a duration."""

    index: int
    provider: str | None
    auth_mode: str | None
    billing: tuple[tuple[str | None, BillingUsage], ...]
    start: datetime | None
    end: datetime

    @property
    def units(self) -> float:
        return sum((weighted_units(self.provider, model, b) for model, b in self.billing),
                   0.0)

    @property
    def gauge(self) -> str | None:
        """The subscription gauge this request drew on; a metered one draws on none."""
        if self.provider is None or is_metered(self.provider, self.auth_mode):
            return None
        return self.provider


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


def increments(events: Iterable[dict[str, Any]]) -> list[Increment]:
    """Every usage-bearing row as one increment, in log order.

    A usage patch and a `query_result` carry no `(provider, model, auth_mode)`
    triple, so each takes its agent's from that agent's last `agent_message`.

    claude `query_result` rows are cumulative within an `(agent, session)` pair,
    so each differences against its predecessor in that pair and a new session
    restarts from zero. claude's own `agent_message` usage is the unreliable
    start-of-message snapshot `build_summary` also skips."""
    triples: dict[str, tuple[str | None, str | None, str | None]] = {}
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
            triples[agent_id] = (p.get("provider"), p.get("model"), p.get("auth_mode"))
            if p.get("provider") == "claude":
                continue
            billing = billing_from_agent_message(p)
            if billing is not None:
                out.append(Increment(
                    index=index, provider=p.get("provider"),
                    auth_mode=p.get("auth_mode"),
                    billing=((p.get("model"), billing),), start=None, end=when))
        elif etype == "agent_message_usage_patch":
            billing = billing_from_usage_patch(p)
            if billing is not None:
                provider, model, auth = triples.get(agent_id, (None, None, None))
                out.append(Increment(
                    index=index, provider=provider, auth_mode=auth,
                    billing=((model, billing),), start=None, end=when))
        else:
            session_id = str(p.get("session_id") or "")
            pair = (agent_id, session_id)
            rollup = p.get("model_usage") or {}
            provider, _model, auth = triples.get(agent_id, (None, None, None))
            delta = _rollup_delta(rollup, previous.get(pair, {}))
            previous[pair] = rollup
            duration_ms = _number(p.get("duration_ms"))
            out.append(Increment(
                index=index, provider=provider, auth_mode=auth,
                billing=tuple(model_rows(delta)),
                start=(when - timedelta(milliseconds=duration_ms)
                       if duration_ms > 0 else None),
                end=when))
    return out


@dataclass(frozen=True)
class Reading:
    """One `7d` gauge reading. Only a reading with a `resets_at` can say which
    week it measured, so no other kind is kept."""

    when: datetime
    provider: str
    used_pct: float
    resets_at: int


def weekly_readings(events: Iterable[dict[str, Any]]) -> list[Reading]:
    out: list[Reading] = []
    for e in events:
        if e.get("type") != "quota_reading":
            continue
        p = e.get("payload") or {}
        provider = p.get("provider")
        when = parse_ts(e.get("timestamp"))
        if not isinstance(provider, str) or when is None:
            continue
        for window in p.get("windows") or []:
            if not isinstance(window, dict) or window.get("label") != _WEEKLY:
                continue
            stamp = window.get("resets_at")
            if isinstance(stamp, int) and not isinstance(stamp, bool):
                out.append(Reading(when, provider, _number(window.get("used_pct")), stamp))
    return out


@dataclass(frozen=True)
class QuotaExtract:
    """One quest's quota facts: a pure function of its own log."""

    increments: tuple[Increment, ...]
    readings: tuple[Reading, ...]


def extract(events: Iterable[dict[str, Any]]) -> QuotaExtract:
    log = list(events)
    return QuotaExtract(tuple(increments(log)), tuple(weekly_readings(log)))


@dataclass(frozen=True)
class WeekSpan:
    """The half-open `[start, end)` an anchor owns: its seven days, minus what an
    earlier anchor's week already holds."""

    anchor: int
    start: datetime
    end: datetime


def week_spans(anchors: Iterable[int]) -> list[WeekSpan]:
    """One span per anchor, in time order. Clipping each start to the previous
    end gives an instant to the smallest anchor whose week encloses it."""
    out: list[WeekSpan] = []
    for anchor in sorted(set(anchors)):
        end = datetime.fromtimestamp(anchor, UTC)
        start = end - _WEEK
        if out and out[-1].end > start:
            start = out[-1].end
        out.append(WeekSpan(anchor, start, end))
    return out


def week_parts(inc: Increment, spans: list[WeekSpan]) -> list[tuple[int | None, float]]:
    """The fraction of `inc` each week holds, by overlap; the share no span
    covers is keyed None. A point increment goes whole to the span holding `end`."""
    ends = [s.end for s in spans]
    if inc.start is None:
        i = bisect_right(ends, inc.end)
        if i < len(spans) and spans[i].start <= inc.end:
            return [(spans[i].anchor, 1.0)]
        return [(None, 1.0)]
    whole = inc.end - inc.start
    parts: list[tuple[int | None, float]] = []
    covered = timedelta()
    for span in spans[bisect_right(ends, inc.start):]:
        if span.start >= inc.end:
            break
        overlap = min(inc.end, span.end) - max(inc.start, span.start)
        if overlap > timedelta():
            parts.append((span.anchor, overlap / whole))
            covered += overlap
    if covered < whole:
        parts.append((None, (whole - covered) / whole))
    return parts


def _spans_by_provider(keys: Iterable[tuple[str, int]]) -> dict[str, list[WeekSpan]]:
    anchors: dict[str, list[int]] = {}
    for provider, anchor in keys:
        anchors.setdefault(provider, []).append(anchor)
    return {provider: week_spans(found) for provider, found in anchors.items()}


def gauge_weeks(subject: QuotaExtract, others: Iterable[QuotaExtract]) -> list[GaugeWeek]:
    """Every gauge-week any quest's readings anchor. The subject's own units are
    left out of `other_units`: its charge adds them per increment."""
    rest = list(others)
    peaks: dict[tuple[str, int], float] = {}
    for found in (subject, *rest):
        for r in found.readings:
            key = (r.provider, r.resets_at)
            peaks[key] = max(peaks.get(key, r.used_pct), r.used_pct)
    spans = _spans_by_provider(peaks)
    other_units = dict.fromkeys(peaks, 0.0)
    for found in rest:
        for inc in found.increments:
            gauge = inc.gauge
            if gauge is None:
                continue
            units = inc.units
            for anchor, share in week_parts(inc, spans.get(gauge, [])):
                if anchor is not None:
                    other_units[(gauge, anchor)] += units * share
    return [GaugeWeek(provider, anchor, peaks[(provider, anchor)],
                      other_units[(provider, anchor)])
            for provider, anchor in sorted(peaks)]


@dataclass(frozen=True)
class GaugeCharge:
    """One gauge's charge: the points its resolved weeks priced (None when none
    did), and whether any part fell in an unmoved week or an unread one."""

    points: float | None
    below: bool
    unread: bool

    @classmethod
    def resolved(cls, points: float) -> GaugeCharge:
        return cls(points, False, False)

    def __add__(self, other: GaugeCharge) -> GaugeCharge:
        points = (None if self.points is None and other.points is None
                  else (self.points or 0.0) + (other.points or 0.0))
        return GaugeCharge(points, self.below or other.below, self.unread or other.unread)


GAUGE_BELOW = GaugeCharge(None, True, False)
GAUGE_UNREAD = GaugeCharge(None, False, True)


@dataclass(frozen=True)
class Charge:
    """A row's or a run's charge per subscription gauge."""

    per_gauge: tuple[tuple[str, GaugeCharge], ...]

    @classmethod
    def of(cls, gauge: str, charge: GaugeCharge) -> Charge:
        return cls(((gauge, charge),))

    @property
    def gauges(self) -> tuple[str, ...]:
        return tuple(g for g, _ in self.per_gauge)

    def __add__(self, other: Charge) -> Charge:
        merged: dict[str, GaugeCharge] = {}
        for gauge, charge in (*self.per_gauge, *other.per_gauge):
            merged[gauge] = merged[gauge] + charge if gauge in merged else charge
        return Charge(tuple(sorted(merged.items(), key=lambda kv: kv[0])))


NO_CHARGE = Charge(())


def _priced_part(amount: float, week: GaugeWeek | None, own: float) -> GaugeCharge:
    if week is None:
        return GAUGE_UNREAD
    if week.peak_pct < _GAUGE_QUANTUM:
        return GAUGE_BELOW
    pool = own + week.other_units
    return GaugeCharge.resolved(amount * week.peak_pct / pool if pool > 0 else 0.0)


def charges_by_index(events: list[dict[str, Any]],
                     weeks: Iterable[GaugeWeek]) -> dict[int, Charge]:
    """Each subscription increment's charge, keyed by its position in `events` —
    the one join a row walk over the same list can rely on, since the persisted
    usage patch carries no event id. Every subscription increment in the log,
    owned by a row or not, counts toward its week's own units."""
    by_key = {(w.provider, w.resets_at): w for w in weeks}
    spans = _spans_by_provider(by_key)
    own: dict[tuple[str, int], float] = {}
    rows: list[tuple[int, str, float, list[tuple[int | None, float]]]] = []
    for inc in increments(events):
        gauge = inc.gauge
        if gauge is None:
            continue
        units = inc.units
        parts = week_parts(inc, spans.get(gauge, []))
        rows.append((inc.index, gauge, units, parts))
        for anchor, share in parts:
            if anchor is not None:
                own[(gauge, anchor)] = own.get((gauge, anchor), 0.0) + units * share
    out: dict[int, Charge] = {}
    for index, gauge, units, parts in rows:
        priced = [_priced_part(units * share,
                               None if anchor is None else by_key[(gauge, anchor)],
                               0.0 if anchor is None else own[(gauge, anchor)])
                  for anchor, share in parts]
        out[index] = Charge.of(gauge, sum(priced[1:], priced[0]))
    return out
