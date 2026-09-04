from __future__ import annotations

import pytest

from imp.providers.catalog import (
    BillingUsage,
    ModelPrices,
    cost_usd,
    prices_for,
    web_search_request_count,
)


def test_anthropic_rates_match_the_published_table() -> None:
    assert prices_for("claude-opus-5") == ModelPrices(5.00, 25.00, 0.50, 6.25)
    assert prices_for("claude-fable-5") == ModelPrices(10.00, 50.00, 1.00, 12.50)
    assert prices_for("claude-opus-4-8") == ModelPrices(5.00, 25.00, 0.50, 6.25)
    assert prices_for("claude-sonnet-4-6") == ModelPrices(3.00, 15.00, 0.30, 3.75)
    assert prices_for("claude-haiku-4-5") == ModelPrices(1.00, 5.00, 0.10, 1.25)


def test_openai_rates_match_the_published_table() -> None:
    assert prices_for("gpt-5.6-sol") == ModelPrices(4.00, 20.00, 0.40, 5.00, 272_000)
    assert prices_for("gpt-5.6-terra") == ModelPrices(2.00, 12.00, 0.20, 2.50, 272_000)
    assert prices_for("gpt-5.6-luna") == ModelPrices(0.20, 1.20, 0.02, 0.25, 272_000)
    assert prices_for("gpt-5.6") == ModelPrices(4.00, 20.00, 0.40, 5.00, 272_000)
    assert prices_for("gpt-5.5") == ModelPrices(5.00, 30.00, 0.50, 5.00, 272_000)
    assert prices_for("gpt-5.4") == ModelPrices(2.50, 15.00, 0.25, 2.50, 272_000)
    assert prices_for("gpt-5.4-mini") == ModelPrices(0.75, 4.50, 0.075, 0.75)
    assert prices_for("gpt-5.3-codex") == ModelPrices(1.75, 14.00, 0.175, 1.75)
    assert prices_for("gpt-5") == ModelPrices(1.25, 10.00, 0.125, 1.25)


def test_cache_write_takes_the_1_25x_premium_only_on_the_5_6_family() -> None:
    for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6"):
        prices = prices_for(model)
        assert prices.cache_write == pytest.approx(prices.input * 1.25)
    for model in ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5"):
        prices = prices_for(model)
        assert prices.cache_write == pytest.approx(prices.input)


def test_lookup_strips_the_bracket_suffix_and_longest_prefix_matches() -> None:
    sonnet = prices_for("claude-sonnet-4-6")
    assert prices_for("claude-opus-4-8[1m]") == prices_for("claude-opus-4-8")
    assert prices_for("claude-opus-5[1m]") == prices_for("claude-opus-5")
    assert prices_for("claude-haiku-4-5-20251001") == prices_for("claude-haiku-4-5")
    assert prices_for("claude-sonnet-4-6-20250219") == sonnet
    # the bare aliases price identically to their dated ids
    assert prices_for("sonnet") == sonnet
    assert prices_for("haiku") == prices_for("claude-haiku-4-5")
    assert prices_for("opus") == prices_for("claude-opus-4-8")
    # exact table keys resolve directly
    assert prices_for("gpt-5.6-sol").input == 4.00
    assert prices_for("gpt-5.4-mini").input == 0.75
    assert prices_for("gpt-5").input == 1.25
    # longest prefix, not first hit: a date-stamped id matches `gpt-5.6-sol`,
    # `gpt-5.6` and `gpt-5` alike, and must take the longest.
    assert prices_for("gpt-5.6-sol-20261201").input == 4.00
    assert prices_for("gpt-5.6-20261201").input == 4.00
    # `gpt-5.4-mini` must not collapse onto the capped `gpt-5.4`
    assert prices_for("gpt-5.4-mini-20261201") == prices_for("gpt-5.4-mini")
    assert prices_for("gpt-5.4-mini-20261201").long_context_threshold is None


def test_an_unknown_model_resolves_to_no_prices() -> None:
    assert prices_for("gpt-9-imaginary") is None
    assert prices_for("") is None
    assert prices_for(None) is None
    assert prices_for("unknown-model[1m]") is None


def test_only_the_surcharged_models_carry_a_long_context_threshold() -> None:
    for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6",
                  "gpt-5.5", "gpt-5.4"):
        assert prices_for(model).long_context_threshold == 272_000
    for model in ("gpt-5.4-mini", "gpt-5.3-codex", "gpt-5", "claude-opus-5",
                  "claude-fable-5", "claude-opus-4-8", "claude-sonnet-4-6",
                  "claude-haiku-4-5"):
        assert prices_for(model).long_context_threshold is None


# 1000 fresh / 2000 cached / 3000 cache write / 4000 output, priced by hand
# against the published per-million rates for every model in the table.
_MIXED = BillingUsage(fresh_input=1000, cached_input=2000,
                      cache_write=3000, output=4000)


@pytest.mark.parametrize(("model", "expected"), [
    ("gpt-5.6-sol", 0.0998),     # 4000 +   800 + 15000 +  80000 per million
    ("gpt-5.6-terra", 0.0579),   # 2000 +   400 +  7500 +  48000
    ("gpt-5.6-luna", 0.00579),   #  200 +    40 +   750 +   4800
    ("gpt-5.6", 0.0998),         # alias -> sol
    ("gpt-5.5", 0.141),          # 5000 +  1000 + 15000 + 120000
    ("gpt-5.4", 0.0705),         # 2500 +   500 +  7500 +  60000
    ("gpt-5.4-mini", 0.02115),   #  750 +   150 +  2250 +  18000
    ("gpt-5.3-codex", 0.06335),  # 1750 +   350 +  5250 +  56000
    ("gpt-5", 0.04525),          # 1250 +   250 +  3750 +  40000
])
def test_every_openai_model_prices_the_hand_worked_fixture(
        model: str, expected: float) -> None:
    assert cost_usd(model, _MIXED) == pytest.approx(expected, rel=1e-12)


def test_cached_input_is_not_billed_at_the_fresh_rate() -> None:
    # 3000 tokens all fresh on gpt-5.6-sol: 3000 * $4/M = $0.012.
    # The same 3000 as 1000 fresh + 2000 cached: 1000 * $4/M + 2000 * $0.40/M.
    assert cost_usd("gpt-5.6-sol",
                    BillingUsage(fresh_input=3000)) == pytest.approx(0.012)
    assert cost_usd("gpt-5.6-sol",
                    BillingUsage(fresh_input=1000, cached_input=2000)
                    ) == pytest.approx(0.0048)


def test_anthropic_buckets_price_side_by_side_with_no_subtraction() -> None:
    # The live probe row: 10 fresh input billed in full ALONGSIDE 12812 cache
    # reads and 52 output on sonnet — 10*$3 + 52*$15 + 12812*$0.30 per million.
    usage = BillingUsage(fresh_input=10, cached_input=12812, output=52)
    assert cost_usd("claude-sonnet-4-6", usage) == pytest.approx(0.0046536)


def test_a_model_absent_from_the_table_has_no_computed_cost() -> None:
    assert cost_usd("gpt-9-imaginary", BillingUsage(fresh_input=1000)) is None


def test_zero_usage_on_a_known_model_costs_nothing() -> None:
    assert cost_usd("gpt-5.6-sol", BillingUsage()) == 0.0


def test_web_search_bills_ten_dollars_per_thousand_requests() -> None:
    # 1000 fresh sonnet input = 1000 * $3/M = $0.003; 250 searches add $2.50.
    tokens_only = BillingUsage(fresh_input=1000)
    with_search = BillingUsage(fresh_input=1000, web_search_requests=250)
    assert cost_usd("claude-sonnet-4-6", tokens_only) == pytest.approx(0.003)
    assert cost_usd("claude-sonnet-4-6", with_search) == pytest.approx(2.503)


def test_web_search_count_reads_the_claude_code_carrier() -> None:
    assert web_search_request_count(
        {"inputTokens": 10, "webSearchRequests": 7}) == 7


def test_web_search_count_reads_the_raw_anthropic_carrier() -> None:
    assert web_search_request_count(
        {"input_tokens": 10,
         "server_tool_use": {"web_search_requests": 4}}) == 4


def test_web_search_count_is_zero_without_a_carrier() -> None:
    assert web_search_request_count(None) == 0
    assert web_search_request_count({}) == 0
    assert web_search_request_count({"input_tokens": 10}) == 0
    assert web_search_request_count({"server_tool_use": None}) == 0
    assert web_search_request_count({"server_tool_use": {}}) == 0
    assert web_search_request_count({"webSearchRequests": 0}) == 0


# 200k fresh + 100k cached + 10k cache write = a 310,000-token prompt, over the
# 272,000 breakpoint; 10k output.
_LONG = BillingUsage(fresh_input=200_000, cached_input=100_000,
                     cache_write=10_000, output=10_000)


def test_the_long_context_surcharge_reprices_the_whole_request() -> None:
    # unsurcharged on gpt-5.6-sol, per million:
    #   200000*$4 + 100000*$0.40 + 10000*$5 + 10000*$20 = $1.09
    assert cost_usd("gpt-5.6-sol", _LONG) == pytest.approx(1.09)
    # surcharged: 2x input, 2x cached, 2x cache write, 1.5x output, everywhere
    #   2*800000 + 2*40000 + 2*50000 + 1.5*200000 per million = $2.08
    assert cost_usd("gpt-5.6-sol", _LONG,
                    prompt_tokens=310_000) == pytest.approx(2.08)


def test_the_surcharge_boundary_is_strictly_above_272k() -> None:
    assert cost_usd("gpt-5.6-sol", _LONG,
                    prompt_tokens=272_000) == pytest.approx(1.09)
    assert cost_usd("gpt-5.6-sol", _LONG,
                    prompt_tokens=272_001) == pytest.approx(2.08)


def test_an_absent_prompt_reading_is_never_surcharged() -> None:
    assert cost_usd("gpt-5.6-sol", _LONG,
                    prompt_tokens=None) == pytest.approx(1.09)


def test_gpt_5_5_and_5_4_are_surcharged_too() -> None:
    # gpt-5.5 unsurcharged: 200000*$5 + 100000*$0.50 + 10000*$5 + 10000*$30
    #   = $1.40; surcharged = 2*1000000 + 2*50000 + 2*50000 + 1.5*300000
    #   per million = $2.65
    assert cost_usd("gpt-5.5", _LONG) == pytest.approx(1.40)
    assert cost_usd("gpt-5.5", _LONG,
                    prompt_tokens=300_000) == pytest.approx(2.65)
    # gpt-5.4 unsurcharged: 200000*$2.50 + 100000*$0.25 + 10000*$2.50
    #   + 10000*$15 = $0.70; surcharged = 2*500000 + 2*25000 + 2*25000
    #   + 1.5*150000 per million = $1.325
    assert cost_usd("gpt-5.4", _LONG) == pytest.approx(0.70)
    assert cost_usd("gpt-5.4", _LONG,
                    prompt_tokens=300_000) == pytest.approx(1.325)


def test_a_model_without_a_threshold_is_never_surcharged() -> None:
    # gpt-5.3-codex, per million: 200000*$1.75 + 100000*$0.175
    #   + 10000*$1.75 + 10000*$14 = $0.525, at any prompt size.
    assert cost_usd("gpt-5.3-codex", _LONG,
                    prompt_tokens=5_000_000) == pytest.approx(0.525)
    for model in ("gpt-5.4-mini", "gpt-5", "claude-opus-5", "claude-sonnet-4-6"):
        assert cost_usd(model, _LONG, prompt_tokens=5_000_000) == pytest.approx(
            cost_usd(model, _LONG))


def test_web_search_is_not_surcharged() -> None:
    # The flat $0.01/request charge sits outside the token multipliers: adding
    # 100 searches raises the surcharged figure by exactly $1.00.
    searched = BillingUsage(fresh_input=200_000, cached_input=100_000,
                            cache_write=10_000, output=10_000,
                            web_search_requests=100)
    assert cost_usd("gpt-5.6-sol", searched,
                    prompt_tokens=310_000) == pytest.approx(3.08)


# A claude_code `model_usage` rollup. Each `costUSD` is hand-worked from the
# PUBLISHED per-million rates, never regenerated from MODEL_PRICES — that is
# what makes this a drift guard rather than a restatement of the table.
#   sonnet:  10*$3 + 52*$15 + 12812*$0.30                        = $0.0046536
#   haiku:   1000*$1 + 2000*$5 + 50000*$0.10 + 4000*$1.25        = $0.021
#   opus:    2000*$5 + 500*$25 + 100000*$0.50 + 8000*$6.25       = $0.1225
#            + 3 web searches * $0.01                            = $0.1525
_MODEL_USAGE = {
    "claude-sonnet-4-6": {
        "inputTokens": 10, "outputTokens": 52,
        "cacheReadInputTokens": 12812, "cacheCreationInputTokens": 0,
        "webSearchRequests": 0, "costUSD": 0.0046536,
    },
    "claude-haiku-4-5-20251001": {
        "inputTokens": 1000, "outputTokens": 2000,
        "cacheReadInputTokens": 50000, "cacheCreationInputTokens": 4000,
        "webSearchRequests": 0, "costUSD": 0.021,
    },
    "claude-opus-4-8[1m]": {
        "inputTokens": 2000, "outputTokens": 500,
        "cacheReadInputTokens": 100000, "cacheCreationInputTokens": 8000,
        "webSearchRequests": 3, "costUSD": 0.1525,
    },
}


def _billing(row: dict[str, float]) -> BillingUsage:
    return BillingUsage(
        fresh_input=int(row["inputTokens"]),
        cached_input=int(row["cacheReadInputTokens"]),
        cache_write=int(row["cacheCreationInputTokens"]),
        output=int(row["outputTokens"]),
        web_search_requests=web_search_request_count(row))


def test_the_table_reproduces_the_vendor_cost_on_every_key_form() -> None:
    for model, row in _MODEL_USAGE.items():
        assert cost_usd(model, _billing(row)) == pytest.approx(
            row["costUSD"], rel=1e-9), model


def test_the_table_reproduces_the_vendor_cost_summed_over_the_rollup() -> None:
    total = sum(cost_usd(model, _billing(row))
                for model, row in _MODEL_USAGE.items())
    assert total == pytest.approx(0.1781536, rel=1e-9)


def test_a_date_stamped_anthropic_id_prices_rather_than_falling_to_n_a() -> None:
    # The raw anthropic path records whatever id the API echoes, date stamp
    # included: 1000*$3 + 1000*$15 per million = $0.018, not None.
    usage = BillingUsage(fresh_input=1000, output=1000)
    assert cost_usd("claude-sonnet-4-6-20250219", usage) == pytest.approx(0.018)
