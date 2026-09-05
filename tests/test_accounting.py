from __future__ import annotations

import pytest

from imp.cli.accounting import (
    RowUsage,
    collapse_cumulative_query_results,
    contribution,
    is_metered,
)
from imp.providers.catalog import BillingUsage


def test_row_usage_adds_every_bucket_and_counter() -> None:
    a = RowUsage(fresh_input=10, cached_input=20, cache_write=30, output=40,
                 dollars=0.25, priced=1, notional=1)
    b = RowUsage(fresh_input=1, cached_input=2, cache_write=3, output=4,
                 dollars=0.75, priced=1, unpriced=1)
    total = a + b
    assert (total.fresh_input, total.cached_input, total.cache_write,
            total.output) == (11, 22, 33, 44)
    assert total.dollars == pytest.approx(1.00)
    assert (total.priced, total.unpriced, total.notional) == (2, 1, 1)


def test_input_total_is_the_whole_input_side_and_excludes_output() -> None:
    usage = RowUsage(fresh_input=10, cached_input=20, cache_write=30, output=40)
    assert usage.input_total == 60


def test_an_empty_row_records_no_model_work() -> None:
    empty = RowUsage()
    assert (empty.priced, empty.unpriced, empty.notional) == (0, 0, 0)
    assert empty.dollars == 0.0


def test_a_priced_contribution_carries_its_dollars() -> None:
    usage = contribution(BillingUsage(fresh_input=100, output=5), 0.42,
                         metered=True)
    assert (usage.fresh_input, usage.output) == (100, 5)
    assert usage.dollars == pytest.approx(0.42)
    assert (usage.priced, usage.unpriced, usage.notional) == (1, 0, 0)


def test_an_unpriced_contribution_keeps_its_tokens_and_no_dollars() -> None:
    # A model absent from the price table: the tokens are real, the dollars
    # are unknowable — never silently 0 in the priced count.
    usage = contribution(BillingUsage(fresh_input=100, output=5), None,
                         metered=True)
    assert usage.fresh_input == 100
    assert usage.dollars == 0.0
    assert (usage.priced, usage.unpriced) == (0, 1)


def test_a_notional_contribution_is_counted_as_such() -> None:
    usage = contribution(BillingUsage(output=5), 0.10, metered=False)
    assert usage.notional == 1


def test_metered_is_provider_aware() -> None:
    # anthropic/openai reach the API by key or not at all — no auth_mode row.
    assert is_metered("anthropic", None) is True
    assert is_metered("openai", None) is True
    # codex and claude_code carry one; anything but apikey is notional.
    assert is_metered("codex", "apikey") is True
    assert is_metered("claude_code", "apikey") is True
    assert is_metered("claude_code", "subscription") is False
    assert is_metered("codex", "chatgpt") is False
    assert is_metered("claude_code", None) is False


def _qr(agent_id: str, session_id: str | None, cost: float) -> dict:
    payload: dict = {"total_cost_usd": cost, "usage": {}, "model_usage": {}}
    if session_id is not None:
        payload["session_id"] = session_id
    return {"type": "query_result", "agent_id": agent_id, "payload": payload}


def test_collapse_keeps_only_the_last_row_of_each_session() -> None:
    events = [
        {"type": "step_started", "payload": {"phase": "p", "step": "s"}},
        _qr("a1", "s1", 5.00),
        _qr("a1", "s1", 12.11),
        _qr("a1", "s2", 4.00),
        _qr("a1", "s2", 16.00),
    ]
    kept = collapse_cumulative_query_results(events)
    assert [e["payload"].get("total_cost_usd") for e in kept
            if e["type"] == "query_result"] == [12.11, 16.00]
    assert kept[0] is events[0]          # every other event passes through


def test_collapse_keeps_sessions_of_different_agents_apart() -> None:
    events = [_qr("a1", "s1", 3.00), _qr("sub:x", "s1", 9.00),
              _qr("a1", "s1", 4.00)]
    kept = collapse_cumulative_query_results(events)
    assert [(e["agent_id"], e["payload"]["total_cost_usd"]) for e in kept] == [
        ("sub:x", 9.00), ("a1", 4.00)]


def test_collapse_folds_an_agents_id_less_rows_into_one_session() -> None:
    # A read-only (cross-project) archive root is never migrated, so its rows
    # carry no session_id. Folding them into ONE session per agent under-counts
    # rather than reinstating the cumulative over-report.
    events = [_qr("a1", None, 3.00), _qr("a1", None, 12.50)]
    kept = collapse_cumulative_query_results(events)
    assert [e["payload"]["total_cost_usd"] for e in kept] == [12.50]


def test_collapse_preserves_event_order_and_leaves_other_types_alone() -> None:
    events = [{"type": "agent_message", "agent_id": "a1", "payload": {}},
              _qr("a1", "s1", 1.0),
              {"type": "step_completed", "payload": {}},
              _qr("a1", "s2", 2.0)]
    kept = collapse_cumulative_query_results(events)
    assert [e["type"] for e in kept] == [
        "agent_message", "query_result", "step_completed", "query_result"]
