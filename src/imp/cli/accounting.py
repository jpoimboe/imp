# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""One accounting path: every provider's usage normalized into the canonical
billing quadruple, priced once, and rolled up per summary row."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from imp.providers.catalog import (
    BillingUsage,
    cost_usd,
    web_search_request_count,
)

_METERED_PROVIDERS = frozenset({"anthropic", "openai"})


@dataclass(frozen=True)
class RowUsage:
    """One row's accounting: the canonical billing quadruple, the dollars that
    could be priced, and the counters the cost cell's state derives from.

    `priced + unpriced` is the row's model work — 0 means it did none.
    """
    fresh_input: int = 0
    cached_input: int = 0
    cache_write: int = 0
    output: int = 0
    dollars: float = 0.0
    priced: int = 0
    unpriced: int = 0
    notional: int = 0

    @property
    def input_total(self) -> int:
        return self.fresh_input + self.cached_input + self.cache_write

    def __add__(self, other: RowUsage) -> RowUsage:
        return RowUsage(
            fresh_input=self.fresh_input + other.fresh_input,
            cached_input=self.cached_input + other.cached_input,
            cache_write=self.cache_write + other.cache_write,
            output=self.output + other.output,
            dollars=self.dollars + other.dollars,
            priced=self.priced + other.priced,
            unpriced=self.unpriced + other.unpriced,
            notional=self.notional + other.notional)


def is_metered(provider: str | None, auth_mode: str | None) -> bool:
    """anthropic/openai reach the API by key or not at all; codex and
    claude carry an auth_mode, and anything but `apikey` is notional."""
    return provider in _METERED_PROVIDERS or auth_mode == "apikey"


def contribution(billing: BillingUsage, dollars: float | None, *,
                 metered: bool) -> RowUsage:
    """One request as a row contribution. `dollars is None` means the model is
    absent from the price table: the tokens still count, the dollars cannot."""
    return RowUsage(
        fresh_input=billing.fresh_input,
        cached_input=billing.cached_input,
        cache_write=billing.cache_write,
        output=billing.output,
        dollars=dollars if dollars is not None else 0.0,
        priced=1 if dollars is not None else 0,
        unpriced=0 if dollars is not None else 1,
        notional=0 if metered else 1)


def collapse_cumulative_query_results(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """claude `query_result` rows are cumulative within one SDK session, so
    only the last row per (agent, session) survives: same id -> take the last,
    different id -> both survive and add. Rows carrying no session id (a
    read-only archive root is never rebuilt) share one bucket per agent, which
    under-counts rather than reinstating the cumulative over-report."""
    last: dict[tuple[str | None, str], int] = {}
    for i, event in enumerate(events):
        if event.get("type") == "query_result":
            payload = event.get("payload") or {}
            last[(event.get("agent_id"), str(payload.get("session_id") or ""))] = i
    keep = set(last.values())
    return [event for i, event in enumerate(events)
            if event.get("type") != "query_result" or i in keep]


def billing_from_agent_message(payload: dict[str, Any]) -> BillingUsage | None:
    """An `agent_message` payload -> the canonical quadruple; None when the row
    carries nothing to bill (codex writes `usage={}` and patches it later)."""
    usage = payload.get("usage")
    if not usage:
        return None
    if payload.get("provider") == "openai":
        return _billed_or_none(billing_usage_from_openai(usage))
    return _billed_or_none(_billing_from_flat(usage))


def billing_from_usage_patch(payload: dict[str, Any]) -> BillingUsage | None:
    """An `agent_message_usage_patch` payload -> the canonical quadruple. codex
    subtracted both cache buckets out of fresh before writing this row, so
    nothing is subtracted again here."""
    usage = payload.get("usage")
    if not usage:
        return None
    return _billed_or_none(_billing_from_flat(usage))


def _billed_or_none(billing: BillingUsage) -> BillingUsage | None:
    """None when nothing in the quadruple is billable. An all-zero usage dict is
    truthy, so the falsy check above does not catch it — and crediting one would
    count a turn that billed nothing as priced model work."""
    if (billing.fresh_input or billing.cached_input or billing.cache_write
            or billing.output or billing.web_search_requests):
        return billing
    return None


def _model_row_billing(row: dict[str, Any]) -> BillingUsage:
    """One `model_usage` entry — the SDK's own camelCase key names."""
    return BillingUsage(
        fresh_input=int(row.get("inputTokens", 0) or 0),
        cached_input=int(row.get("cacheReadInputTokens", 0) or 0),
        cache_write=int(row.get("cacheCreationInputTokens", 0) or 0),
        output=int(row.get("outputTokens", 0) or 0),
        web_search_requests=web_search_request_count(row))


def model_rows(model_usage: dict[str, Any]) -> list[tuple[str, BillingUsage]]:
    """The rollup as one (model id, quadruple) pair per model, so a fallback can
    price each id separately."""
    return [(str(model), _model_row_billing(row))
            for model, row in model_usage.items() if isinstance(row, dict)]


def billing_from_model_usage(model_usage: dict[str, Any]) -> BillingUsage:
    """A claude `query_result.model_usage` rollup -> one quadruple over
    every model in it, the background haiku included.

    The web-search count rides along on both paths, so "billable" means the same
    thing either way; it is priced only where the vendor's own cost is missing,
    since that figure already folds web search in.
    """
    total = BillingUsage()
    for _model, row in model_rows(model_usage):
        total = BillingUsage(
            fresh_input=total.fresh_input + row.fresh_input,
            cached_input=total.cached_input + row.cached_input,
            cache_write=total.cache_write + row.cache_write,
            output=total.output + row.output,
            web_search_requests=(total.web_search_requests
                                 + row.web_search_requests))
    return total


def vendor_contribution(model_usage: dict[str, Any],
                        total_cost_usd: float | None, *,
                        metered: bool) -> RowUsage | None:
    """A claude `query_result` as a row contribution: the vendor's own cost
    when it carries one, else each model in the rollup priced from the table.
    None when the row carries neither dollars nor billable usage —
    `total_cost_usd` is `float | None` on the wire, so that row is reachable."""
    if total_cost_usd is not None:
        return contribution(billing_from_model_usage(model_usage),
                            float(total_cost_usd), metered=metered)
    total: RowUsage | None = None
    for model, billing in model_rows(model_usage):
        if _billed_or_none(billing) is None:
            continue
        credit = computed_contribution(model, billing, None, metered=metered)
        total = credit if total is None else total + credit
    return total


def billing_usage_from_openai(usage: dict[str, Any] | None) -> BillingUsage:
    """A raw `ResponseUsage` dump → the canonical quadruple."""
    if not usage:
        return BillingUsage()
    details = usage.get("input_tokens_details")
    cached = (int(details.get("cached_tokens", 0) or 0)
              if isinstance(details, dict) else 0)
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    return BillingUsage(
        fresh_input=max(0, input_tokens - cached),  # input_tokens includes cached; fresh is net
        cached_input=cached,
        # reasoning_tokens is inside output_tokens; never added on top
        output=int(usage.get("output_tokens", 0) or 0))


def _billing_from_flat(usage: dict[str, Any]) -> BillingUsage:
    """The Anthropic-shaped flat keys, whose cache buckets are disjoint from
    `input_tokens` — the shape codex normalizes its patch row into as well."""
    return BillingUsage(
        fresh_input=int(usage.get("input_tokens", 0) or 0),
        cached_input=int(usage.get("cache_read_input_tokens", 0) or 0),
        cache_write=int(usage.get("cache_creation_input_tokens", 0) or 0),
        output=int(usage.get("output_tokens", 0) or 0),
        web_search_requests=web_search_request_count(usage))


def computed_contribution(model: str | None, billing: BillingUsage,
                          prompt_tokens: int | None, *,
                          metered: bool) -> RowUsage:
    """A contribution priced from the table — every provider, claude
    included whenever its own dollar figure never arrived."""
    return contribution(billing, cost_usd(model, billing, prompt_tokens),
                        metered=metered)
