from __future__ import annotations

import pytest

from imp.cli.accounting import (
    RowUsage,
    billing_from_agent_message,
    billing_from_model_usage,
    billing_from_usage_patch,
    billing_usage_from_openai,
    collapse_cumulative_query_results,
    contribution,
    is_metered,
    vendor_contribution,
)
from imp.providers.catalog import BillingUsage, cost_usd


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
    # codex and claude carry one; anything but apikey is notional.
    assert is_metered("codex", "apikey") is True
    assert is_metered("claude", "apikey") is True
    assert is_metered("claude", "subscription") is False
    assert is_metered("codex", "chatgpt") is False
    assert is_metered("claude", None) is False


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
    # A read-only (cross-project) archive root is never rebuilt, so its rows
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


def test_anthropic_usage_bills_input_in_full_beside_its_cache_buckets() -> None:
    # AC 9(c): the buckets are disjoint on this path — nothing is subtracted.
    billing = billing_from_agent_message({
        "provider": "anthropic", "model": "claude-sonnet-4-6",
        "usage": {"input_tokens": 1000, "output_tokens": 500,
                  "cache_read_input_tokens": 2000,
                  "cache_creation_input_tokens": 300,
                  "server_tool_use": {"web_search_requests": 2}}})
    assert billing == BillingUsage(fresh_input=1000, cached_input=2000,
                                   cache_write=300, output=500,
                                   web_search_requests=2)


def test_openai_usage_is_normalized_out_of_its_nested_shape() -> None:
    # AC 10: cached is subtracted out of fresh (1000 - 600), and reasoning is
    # already inside output_tokens, so output stays 400 rather than 650.
    billing = billing_from_agent_message({
        "provider": "openai", "model": "gpt-6.1-sol",
        "usage": {"input_tokens": 1000,
                  "input_tokens_details": {"cached_tokens": 600},
                  "output_tokens": 400,
                  "output_tokens_details": {"reasoning_tokens": 250}}})
    assert billing == BillingUsage(fresh_input=400, cached_input=600,
                                   cache_write=0, output=400)


def test_a_usage_less_agent_message_bills_nothing() -> None:
    # codex writes usage={} on the message and the real figures on the patch.
    assert billing_from_agent_message(
        {"provider": "codex", "model": "gpt-6.1-sol", "usage": {}}) is None
    assert billing_from_agent_message({"provider": "anthropic"}) is None


def test_the_usage_patch_row_is_taken_verbatim_with_no_second_subtraction() -> None:
    # AC 9(b): codex_runner already wrote fresh = input - cached - cache_write,
    # so 300 must survive here; subtracting again would clamp it toward 0.
    billing = billing_from_usage_patch({
        "usage": {"input_tokens": 300, "output_tokens": 50,
                  "cache_read_input_tokens": 600,
                  "cache_creation_input_tokens": 100,
                  "reasoning_output_tokens": 20},
        "context_tokens": 5000})
    assert billing == BillingUsage(fresh_input=300, cached_input=600,
                                   cache_write=100, output=50)


def test_an_empty_usage_patch_bills_nothing() -> None:
    assert billing_from_usage_patch({"usage": {}, "context_tokens": 10}) is None


def test_an_all_zero_quadruple_is_not_billable_work() -> None:
    # An all-zero usage dict is a real on-disk shape — the "empty reply" of a
    # freshly-resumed session (`narrative.py:867`). It is truthy, so a falsy
    # check alone would let it through and count a turn that billed nothing as
    # priced model work, flipping the renderer's "step did no model work" state.
    zero = {"input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    assert billing_from_agent_message(
        {"provider": "anthropic", "model": "m", "usage": zero}) is None
    assert billing_from_agent_message(
        {"provider": "openai", "model": "m",
         "usage": {"input_tokens": 0, "output_tokens": 0}}) is None
    assert billing_from_usage_patch({"usage": zero}) is None


def test_a_web_search_with_no_tokens_is_still_billable_work() -> None:
    # $10/1,000 requests is a flat charge that does not need a token to exist.
    billing = billing_from_agent_message({
        "provider": "anthropic", "model": "claude-sonnet-4-6",
        "usage": {"input_tokens": 0, "output_tokens": 0,
                  "server_tool_use": {"web_search_requests": 2}}})
    assert billing == BillingUsage(web_search_requests=2)


def test_model_usage_sums_every_model_including_haiku() -> None:
    # AC 2(b): haiku is present only in model_usage — the background model never
    # appears in the agent_message stream at all. The web-search count rides
    # along: when the vendor omits its own cost, the fallback prices it, and
    # dropping it here would under-bill a search-heavy row by $0.01 a request.
    billing = billing_from_model_usage({
        "claude-sonnet-4-6": {"inputTokens": 100, "outputTokens": 50,
                              "cacheReadInputTokens": 1000,
                              "cacheCreationInputTokens": 200,
                              "webSearchRequests": 3, "costUSD": 0.40},
        "claude-haiku-4-5-20251001": {"inputTokens": 10, "outputTokens": 5,
                                      "cacheReadInputTokens": 100,
                                      "cacheCreationInputTokens": 0,
                                      "webSearchRequests": 0, "costUSD": 0.02}})
    assert billing == BillingUsage(fresh_input=110, cached_input=1100,
                                   cache_write=200, output=55,
                                   web_search_requests=3)


def test_an_empty_model_usage_rollup_bills_nothing() -> None:
    assert billing_from_model_usage({}) == BillingUsage()


def test_a_query_result_with_neither_dollars_nor_usage_is_not_work() -> None:
    # `total_cost_usd` is `float | None` on the wire (`state.py:598`), so a row
    # can carry no cost at all. With an empty rollup beside it there is nothing
    # to bill, and crediting it would count a query that billed nothing as
    # priced model work — the same category error the quadruple guard prevents.
    assert vendor_contribution({}, None, metered=False) is None


def test_a_query_result_counts_when_either_half_is_present() -> None:
    # The vendor's cost alone is billable work even with an empty rollup...
    cost_only = vendor_contribution({}, 0.42, metered=False)
    assert cost_only is not None
    assert cost_only.dollars == pytest.approx(0.42)
    assert (cost_only.priced, cost_only.notional) == (1, 1)
    # ...and so are tokens alone, which the table now prices: sonnet, per
    # million, 100*$3 + 5*$15 = $0.000375.
    tokens_only = vendor_contribution(
        {"claude-sonnet-4-6": {"inputTokens": 100, "outputTokens": 5}},
        None, metered=True)
    assert tokens_only is not None
    assert (tokens_only.fresh_input, tokens_only.output) == (100, 5)
    assert tokens_only.dollars == pytest.approx(0.000375)
    assert (tokens_only.priced, tokens_only.unpriced) == (1, 0)


def test_a_vendor_less_rollup_prices_every_model_against_the_table() -> None:
    # No total_cost_usd on the wire: each model id is priced separately. Per
    # million — sonnet 1000*$3 + 1000*$15 = $0.018, haiku $1/$5 = $0.006,
    # opus $5/$25 = $0.030.
    usage = vendor_contribution({
        "claude-sonnet-4-6": {"inputTokens": 1000, "outputTokens": 1000},
        "claude-haiku-4-5-20251001": {"inputTokens": 1000, "outputTokens": 1000},
        "claude-opus-5": {"inputTokens": 1000, "outputTokens": 1000},
    }, None, metered=False)
    assert usage is not None
    assert usage.dollars == pytest.approx(0.054)
    assert (usage.priced, usage.unpriced) == (3, 0)
    assert (usage.fresh_input, usage.output) == (3000, 3000)


def test_a_vendor_less_rollup_floors_on_a_model_absent_from_the_table() -> None:
    usage = vendor_contribution({
        "claude-sonnet-4-6": {"inputTokens": 5000, "outputTokens": 8000,
                              "cacheReadInputTokens": 200_000},
        "claude-mythic-9": {"inputTokens": 2000, "outputTokens": 1000},
    }, None, metered=False)
    assert usage is not None
    # 5,000*$3 + 200,000*$0.30 + 8,000*$15 per million; mythic adds no dollars.
    assert usage.dollars == pytest.approx(0.195)
    assert (usage.priced, usage.unpriced) == (1, 1)
    assert usage.fresh_input == 7000       # the unpriced model's tokens are real


def test_a_vendor_less_rollup_of_unknown_models_carries_no_dollars() -> None:
    usage = vendor_contribution(
        {"claude-mythic-9": {"inputTokens": 2000, "outputTokens": 1000}},
        None, metered=False)
    assert usage is not None
    assert (usage.priced, usage.unpriced) == (0, 1)
    assert usage.dollars == 0.0


def test_a_vendor_figure_wins_over_what_the_table_would_compute() -> None:
    # The table would say $0.000375 for these tokens; the vendor says $0.99, and
    # the two differing numbers prove the table was never consulted.
    usage = vendor_contribution(
        {"claude-sonnet-4-6": {"inputTokens": 100, "outputTokens": 5}},
        0.99, metered=True)
    assert usage is not None
    assert usage.dollars == pytest.approx(0.99)


def test_a_vendor_zero_is_a_vendor_figure_not_a_missing_one() -> None:
    usage = vendor_contribution(
        {"claude-sonnet-4-6": {"inputTokens": 100, "outputTokens": 5}},
        0.0, metered=True)
    assert usage is not None
    assert usage.dollars == 0.0             # not the table's $0.000375
    assert (usage.priced, usage.unpriced) == (1, 0)


def test_a_vendor_figure_is_unmoved_by_the_web_search_count() -> None:
    # The vendor's dollars already fold web search in, so carrying the count
    # into the quadruple must not add $0.07 on top of it.
    usage = vendor_contribution(
        {"claude-sonnet-4-6": {"inputTokens": 100, "outputTokens": 5,
                               "webSearchRequests": 7}},
        0.42, metered=True)
    assert usage is not None
    assert usage.dollars == pytest.approx(0.42)


def test_an_all_zero_vendor_less_rollup_is_not_work() -> None:
    assert vendor_contribution(
        {"claude-sonnet-4-6": {"inputTokens": 0, "outputTokens": 0,
                               "cacheReadInputTokens": 0,
                               "cacheCreationInputTokens": 0,
                               "webSearchRequests": 0}},
        None, metered=False) is None


def test_openai_cached_tokens_are_not_billed_at_the_fresh_rate() -> None:
    """OpenAI's `input_tokens` INCLUDES cached tokens, so fresh must be net of
    them: 100k input with 90k cached is 10k fresh, not 100k."""
    usage = {
        "input_tokens": 100_000,
        "input_tokens_details": {"cached_tokens": 90_000},
        "output_tokens": 2_000,
        "output_tokens_details": {"reasoning_tokens": 1_500},
        "total_tokens": 102_000,
    }
    billing = billing_usage_from_openai(usage)
    assert billing.fresh_input == 10_000
    assert billing.cached_input == 90_000
    # ResponseUsage carries no cache-write field at all.
    assert billing.cache_write == 0
    # 10_000*2.00 + 90_000*0.10 + 2_000*10.00 = 49_000 per million.
    assert cost_usd("gpt-6.1-sol", billing) == pytest.approx(0.049)
    # Billing all 100k as fresh would be $0.22 — the bug this closes.
    assert cost_usd("gpt-6.1-sol", billing) != pytest.approx(0.22)


def test_openai_reasoning_tokens_are_not_added_on_top_of_output() -> None:
    """Reasoning is billed as output and already counted in `output_tokens`."""
    billing = billing_usage_from_openai({
        "input_tokens": 500,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 4_000,
        "output_tokens_details": {"reasoning_tokens": 3_800},
        "total_tokens": 4_500,
    })
    assert billing.output == 4_000


def test_a_flat_openai_usage_bills_every_input_token_as_fresh() -> None:
    """_usage_dict fallback emits no nested detail; nothing to subtract."""
    billing = billing_usage_from_openai(
        {"input_tokens": 700, "output_tokens": 30, "total_tokens": 730})
    assert billing.fresh_input == 700
    assert billing.cached_input == 0
    assert billing.output == 30


def test_an_absent_openai_usage_bills_nothing() -> None:
    assert billing_usage_from_openai({}) == BillingUsage()
    assert billing_usage_from_openai(None) == BillingUsage()
