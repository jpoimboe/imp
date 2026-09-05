# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""One accounting path: every provider's usage normalized into the canonical
billing quadruple, priced once, and rolled up per summary row."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from imp.providers.catalog import BillingUsage

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
    claude_code carry an auth_mode, and anything but `apikey` is notional."""
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
    """claude_code `query_result` rows are cumulative within one SDK session, so
    only the last row per (agent, session) survives: same id -> take the last,
    different id -> both survive and add. Rows carrying no session id (a
    read-only archive root is never migrated) share one bucket per agent, which
    under-counts rather than reinstating the cumulative over-report."""
    last: dict[tuple[str | None, str], int] = {}
    for i, event in enumerate(events):
        if event.get("type") == "query_result":
            payload = event.get("payload") or {}
            last[(event.get("agent_id"), str(payload.get("session_id") or ""))] = i
    keep = set(last.values())
    return [event for i, event in enumerate(events)
            if event.get("type") != "query_result" or i in keep]
