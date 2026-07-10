from __future__ import annotations

from imp.providers.catalog import (
    ANTHROPIC,
    CLAUDE_CODE,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_WINDOW,
    OPENAI,
    ModelInfo,
    catalog_aliases,
    checkpoint_fraction,
    context_tokens_from_usage,
    model_info,
    model_supports_vision,
    window_for_model,
)


def test_claude_code_catalog_opus_1m_entry() -> None:
    info = model_info(CLAUDE_CODE, "claude-opus-4-8[1m]")
    assert info.window == 1_000_000
    # The bracketed alias IS the CLI model id: it requests 1M under both
    # subscription and API-key auth; no custom beta (which subscription auth
    # silently drops) and no cli_model indirection.
    assert info.betas == ()
    assert info.cli_model is None


def test_anthropic_catalog_opus_1m_entry() -> None:
    info = model_info(ANTHROPIC, "claude-opus-4-8[1m]")
    assert info.window == 1_000_000
    assert info.max_output_tokens == 64_000
    # The raw API rejects the bracketed form: cli_model strips it and the
    # context-1m beta header requests the 1M window.
    assert info.cli_model == "claude-opus-4-8"
    assert info.betas == ("context-1m-2025-08-07",)


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
    # The "[1m]" marker must win over the base-model token it accompanies.
    assert window_for_model("claude-opus-4-8[1m]") == 1_000_000
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


def test_anthropic_max_output_tokens_per_model() -> None:
    # Opus class is the 64000 baseline; sonnet/haiku class is 32000.
    assert model_info(ANTHROPIC, "opus").max_output_tokens == 64_000
    assert model_info(ANTHROPIC, "sonnet").max_output_tokens == 32_000
    assert model_info(ANTHROPIC, "haiku").max_output_tokens == 32_000


def test_default_max_output_tokens_constant_is_32000() -> None:
    assert DEFAULT_MAX_OUTPUT_TOKENS == 32_000


def test_dated_anthropic_id_falls_to_default_max_output_tokens() -> None:
    # An operator-pinned dated id misses the alias table and resolves through
    # _DEFAULT to the sonnet/haiku-class 32000, never an unspecified or zero value.
    assert model_info(ANTHROPIC, "claude-opus-4-8-20260601").max_output_tokens == 32_000
    assert model_info(ANTHROPIC, "claude-sonnet-4-6-20260601").max_output_tokens == 32_000
    # The undated major.minor ids (the tier-floor pins) ARE in the table, so
    # opus keeps its 64k there.
    assert model_info(ANTHROPIC, "claude-opus-4-8").max_output_tokens == 64_000


def test_model_info_defaults_to_vision_capable() -> None:
    assert ModelInfo().supports_vision is True


def test_known_models_are_vision_capable() -> None:
    assert model_supports_vision("anthropic", "opus") is True
    assert model_supports_vision("claude_code", "sonnet") is True
    assert model_supports_vision("openai", "gpt-5") is True
    assert model_supports_vision("codex", "gpt-5.3-codex") is True


def test_unknown_provider_or_model_is_vision_capable() -> None:
    # Unknown provider name → no catalog → assume vision (True).
    assert model_supports_vision("mystery", "opus") is True
    # Known provider, unknown alias → model_info default → True.
    assert model_supports_vision("anthropic", "no-such-model") is True


def test_text_only_flag_forces_no_vision(monkeypatch) -> None:
    # A model explicitly flagged supports_vision=False resolves to False.
    monkeypatch.setitem(ANTHROPIC, "text-only", ModelInfo(supports_vision=False))
    assert model_supports_vision("anthropic", "text-only") is False


def test_claude_code_catalog_has_fable_entries() -> None:
    # Both aliases are present in the table (not merely resolved via the
    # conservative default), window-only like the existing opus entries.
    assert "claude-fable-5" in CLAUDE_CODE
    assert "claude-fable-5[1m]" in CLAUDE_CODE
    assert CLAUDE_CODE["claude-fable-5"] == ModelInfo(window=200_000)
    assert CLAUDE_CODE["claude-fable-5[1m]"] == ModelInfo(window=1_000_000)


def test_anthropic_catalog_has_fable_entries() -> None:
    # Plain: 200k window, 128k output cap, no cli_model/betas indirection.
    assert "claude-fable-5" in ANTHROPIC
    assert ANTHROPIC["claude-fable-5"] == ModelInfo(
        window=200_000, max_output_tokens=128_000
    )
    # 1M: raw API rejects the bracketed form, so cli_model strips it and the
    # context-1m beta header requests the 1M window; 128k output cap.
    assert "claude-fable-5[1m]" in ANTHROPIC
    assert ANTHROPIC["claude-fable-5[1m]"] == ModelInfo(
        window=1_000_000,
        max_output_tokens=128_000,
        cli_model="claude-fable-5",
        betas=("context-1m-2025-08-07",),
    )


def test_window_for_model_covers_fable() -> None:
    # The "[1m]" marker still wins; the plain fable alias resolves to 200k via
    # the new substring token (keeping _WINDOW_TOKENS in agreement with the
    # structured tables per the module NOTE).
    assert window_for_model("claude-fable-5[1m]") == 1_000_000
    assert window_for_model("claude-fable-5") == 200_000


def test_catalog_aliases_lists_provider_table_keys() -> None:
    anthropic = catalog_aliases("anthropic")
    assert "claude-fable-5[1m]" in anthropic
    assert "claude-fable-5" in anthropic
    assert "sonnet" in catalog_aliases("claude_code")
    # codex shares the OPENAI table
    assert catalog_aliases("codex") == catalog_aliases("openai")
    assert "gpt-5" in catalog_aliases("openai")


def test_catalog_aliases_unknown_provider_is_empty() -> None:
    assert catalog_aliases("nope") == ()
