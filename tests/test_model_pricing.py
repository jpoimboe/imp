from __future__ import annotations

import pytest

from imp.providers.catalog import (
    BillingUsage,
    ModelPrices,
    cost_usd,
    prices_for,
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
