from __future__ import annotations

from imp.providers.catalog import (
    ANTHROPIC,
    CLAUDE_CODE,
    DEFAULT_WINDOW,
    OPENAI,
    ModelInfo,
    checkpoint_fraction,
    context_tokens_from_usage,
    model_info,
    window_for_model,
)


def test_claude_code_catalog_opus_1m_entry() -> None:
    info = model_info(CLAUDE_CODE, "opus-1m")
    assert info.window == 1_000_000
    # The bracketed model-id requests 1M under both subscription and API-key
    # auth; no custom beta (which subscription auth silently drops).
    assert info.betas == ()
    assert info.cli_model == "claude-opus-4-8[1m]"


def test_claude_code_catalog_opus_entry_is_plain_200k() -> None:
    info = model_info(CLAUDE_CODE, "opus")
    assert info.window == 200_000
    assert info.betas == ()
    assert info.cli_model is None


def test_unknown_alias_yields_conservative_default() -> None:
    info = model_info(CLAUDE_CODE, "claude-sonnet-4-5")
    assert info == ModelInfo(window=DEFAULT_WINDOW)
    assert info.betas == ()
    assert info.cli_model is None


def test_other_providers_seed_their_windows() -> None:
    assert model_info(ANTHROPIC, "opus").window == 200_000
    assert model_info(OPENAI, "gpt-5.5").window == 400_000


def test_window_for_model_substring_fallback() -> None:
    # opus-1m must win over opus despite "opus" being a substring of "opus-1m".
    assert window_for_model("opus-1m") == 1_000_000
    assert window_for_model("opus") == 200_000
    assert window_for_model("haiku") == 200_000
    assert window_for_model("claude-opus-4-1") == 200_000
    assert window_for_model("claude-sonnet-4-5") == 200_000
    assert window_for_model("gpt-5.5") == 400_000
    assert window_for_model("some-unlisted-model") is None
    assert window_for_model(None) is None


def test_checkpoint_fraction_fractional_threshold_passes_through() -> None:
    # A window fraction is window-independent: returned unchanged.
    assert checkpoint_fraction(0.8, 200_000) == 0.8
    assert checkpoint_fraction(0.8, None) == 0.8
    assert checkpoint_fraction(1.0, 1_000_000) == 1.0


def test_checkpoint_fraction_absolute_threshold_divides_by_window() -> None:
    # An absolute token cap becomes a fraction of the model window.
    assert checkpoint_fraction(150_000, 200_000) == 0.75
    assert checkpoint_fraction(500_000, 1_000_000) == 0.5


def test_checkpoint_fraction_unresolvable_returns_none() -> None:
    assert checkpoint_fraction(None, 200_000) is None       # no threshold
    assert checkpoint_fraction(0, 200_000) is None          # non-positive
    assert checkpoint_fraction(150_000, None) is None        # absolute cap, unknown window
    assert checkpoint_fraction(150_000, 0) is None           # absolute cap, zero window
    assert checkpoint_fraction(-1, 200_000) is None        # negative threshold


def test_context_threshold_crossed_fires_at_fraction() -> None:
    from imp.providers.catalog import context_threshold_crossed
    # 170000/200000 = 0.85 >= 0.8 -> crossed.
    assert context_threshold_crossed(
        {"input_tokens": 170_000}, "sonnet", 0.8) is True


def test_context_threshold_crossed_not_before_fraction() -> None:
    from imp.providers.catalog import context_threshold_crossed
    # 150000/200000 = 0.75 < 0.8 -> not crossed.
    assert context_threshold_crossed(
        {"input_tokens": 150_000}, "sonnet", 0.8) is False


def test_context_threshold_crossed_sums_cache_tokens() -> None:
    from imp.providers.catalog import context_threshold_crossed
    # 100000 + 60000 + 5000 = 165000 / 200000 = 0.825 >= 0.8.
    assert context_threshold_crossed(
        {"input_tokens": 100_000, "cache_read_input_tokens": 60_000,
         "cache_creation_input_tokens": 5_000}, "sonnet", 0.8) is True


def test_context_threshold_crossed_unknown_window_never_fires() -> None:
    from imp.providers.catalog import context_threshold_crossed
    assert context_threshold_crossed(
        {"input_tokens": 999_999}, "mystery-model", 0.8) is False


def test_context_threshold_crossed_absolute_cap_divides_by_window() -> None:
    from imp.providers.catalog import context_threshold_crossed
    # Absolute cap 150000 on a 200000 window -> warn fraction 0.75; 160000/200000
    # = 0.80 >= 0.75 -> crossed.
    assert context_threshold_crossed(
        {"input_tokens": 160_000}, "sonnet", 150_000) is True


def test_context_threshold_crossed_no_threshold_or_usage_never_fires() -> None:
    from imp.providers.catalog import context_threshold_crossed
    assert context_threshold_crossed({"input_tokens": 999_999}, "sonnet", None) is False
    assert context_threshold_crossed(None, "sonnet", 0.8) is False
    assert context_threshold_crossed({}, "sonnet", 0.8) is False


def test_sdk_context_threshold_crossed_fires_at_fraction() -> None:
    from imp.providers.catalog import sdk_context_threshold_crossed
    # 170000/200000 = 0.85 >= 0.8 -> crossed.
    assert sdk_context_threshold_crossed(170_000, 200_000, 0.8) is True


def test_sdk_context_threshold_crossed_not_before_fraction() -> None:
    from imp.providers.catalog import sdk_context_threshold_crossed
    # 150000/200000 = 0.75 < 0.8 -> not crossed.
    assert sdk_context_threshold_crossed(150_000, 200_000, 0.8) is False


def test_sdk_context_threshold_crossed_absolute_cap_divides_by_window() -> None:
    from imp.providers.catalog import sdk_context_threshold_crossed
    # Absolute cap 150000 on a 200000 window -> cutoff 0.75; 160000/200000
    # = 0.80 >= 0.75 -> crossed.
    assert sdk_context_threshold_crossed(160_000, 200_000, 150_000) is True


def test_sdk_context_threshold_crossed_unavailable_inputs_never_fire() -> None:
    from imp.providers.catalog import sdk_context_threshold_crossed
    assert sdk_context_threshold_crossed(None, 200_000, 0.8) is False
    assert sdk_context_threshold_crossed(170_000, None, 0.8) is False
    assert sdk_context_threshold_crossed(170_000, 200_000, None) is False
    assert sdk_context_threshold_crossed(0, 200_000, 0.8) is False
    assert sdk_context_threshold_crossed(170_000, 0, 0.8) is False


def test_context_tokens_from_usage_anthropic_cache_split() -> None:
    # Anthropic input_tokens EXCLUDES cached tokens, so the true prompt size is
    # the three-way sum: 1000 + 5000 + 200 = 6200.
    usage = {
        "input_tokens": 1000,
        "cache_read_input_tokens": 5000,
        "cache_creation_input_tokens": 200,
        "output_tokens": 50,
    }
    assert context_tokens_from_usage(usage) == 6200


def test_context_tokens_from_usage_openai_inclusive() -> None:
    # OpenAI Responses carries no cache fields, so the sum reduces to
    # input_tokens (which already includes cache). Not total_tokens, not output.
    usage = {"input_tokens": 1234, "output_tokens": 99, "total_tokens": 1333}
    assert context_tokens_from_usage(usage) == 1234


def test_context_tokens_from_usage_empty_is_zero() -> None:
    assert context_tokens_from_usage({}) == 0
    assert context_tokens_from_usage(None) == 0
