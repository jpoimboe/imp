from __future__ import annotations

import pytest

from imp.providers.catalog import ModelPrices, prices_for


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
