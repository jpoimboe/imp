from __future__ import annotations

from imp.providers.catalog import (
    ANTHROPIC,
    CLAUDE_CODE,
    DEFAULT_CHECKPOINT_FRACTION,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_WINDOW,
    OPENAI,
    ModelInfo,
    catalog_aliases,
    checkpoint_cutoff,
    context_tokens_from_usage,
    limits_for,
    model_info,
    model_supports_vision,
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
    assert model_info(OPENAI, "gpt-5.5").window == 1_050_000


def test_context_threshold_crossed_fires_at_the_default_fraction() -> None:
    from imp.providers.catalog import context_threshold_crossed
    # 170000/200000 = 0.85 >= 0.8 -> crossed.
    assert context_threshold_crossed(
        {"input_tokens": 170_000}, "anthropic", "sonnet") is True


def test_context_threshold_crossed_not_before_the_fraction() -> None:
    from imp.providers.catalog import context_threshold_crossed
    # 150000/200000 = 0.75 < 0.8 -> not crossed.
    assert context_threshold_crossed(
        {"input_tokens": 150_000}, "anthropic", "sonnet") is False


def test_context_threshold_crossed_sums_cache_tokens() -> None:
    from imp.providers.catalog import context_threshold_crossed
    # 100000 + 60000 + 5000 = 165000 / 200000 = 0.825 >= 0.8.
    assert context_threshold_crossed(
        {"input_tokens": 100_000, "cache_read_input_tokens": 60_000,
         "cache_creation_input_tokens": 5_000}, "anthropic", "sonnet") is True


def test_context_threshold_crossed_unknown_model_or_provider_never_fires() -> None:
    from imp.providers.catalog import context_threshold_crossed
    assert context_threshold_crossed(
        {"input_tokens": 999_999}, "openai", "mystery-model") is False
    assert context_threshold_crossed(
        {"input_tokens": 999_999}, "mystery", "sonnet") is False


def test_context_threshold_crossed_without_usage_never_fires() -> None:
    from imp.providers.catalog import context_threshold_crossed
    assert context_threshold_crossed(None, "anthropic", "sonnet") is False
    assert context_threshold_crossed({}, "anthropic", "sonnet") is False


def test_gpt_5_6_crosses_at_the_260k_soft_cap_on_both_providers() -> None:
    from imp.providers.catalog import context_threshold_crossed
    # The whole point of the quest: 260k, not 0.8 * 1.05M = 840k.
    for provider in ("openai", "codex"):
        for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6"):
            assert context_threshold_crossed(
                {"input_tokens": 261_000}, provider, model) is True
            assert context_threshold_crossed(
                {"input_tokens": 259_000}, provider, model) is False
            # deep into the old cutoff's territory, but still under 840k
            assert context_threshold_crossed(
                {"input_tokens": 800_000}, provider, model) is True


def test_claude_opus_5_1m_keeps_its_800k_crossing() -> None:
    from imp.providers.catalog import context_threshold_crossed
    # 0.8 of the 1M window; the OpenAI cap must not leak across providers.
    for provider in ("anthropic", "claude_code"):
        assert context_threshold_crossed(
            {"input_tokens": 810_000}, provider, "claude-opus-5[1m]") is True
        assert context_threshold_crossed(
            {"input_tokens": 790_000}, provider, "claude-opus-5[1m]") is False
        assert context_threshold_crossed(
            {"input_tokens": 300_000}, provider, "claude-opus-5[1m]") is False


def test_sdk_crossed_applies_the_model_soft_cap() -> None:
    from imp.providers.catalog import sdk_context_threshold_crossed
    # Same 260k cap, measured against the SDK's own live window.
    assert sdk_context_threshold_crossed(
        261_000, 1_050_000, "openai", "gpt-5.6-sol") is True
    assert sdk_context_threshold_crossed(
        259_000, 1_050_000, "openai", "gpt-5.6-sol") is False


def test_sdk_crossed_uncapped_model_uses_the_default_fraction() -> None:
    from imp.providers.catalog import sdk_context_threshold_crossed
    # 170000/200000 = 0.85 >= 0.8; 150000/200000 = 0.75 < 0.8.
    assert sdk_context_threshold_crossed(
        170_000, 200_000, "claude_code", "opus") is True
    assert sdk_context_threshold_crossed(
        150_000, 200_000, "claude_code", "opus") is False


def test_sdk_crossed_unknown_model_trusts_the_live_window() -> None:
    from imp.providers.catalog import sdk_context_threshold_crossed
    # The SDK's window is authoritative, so an unknown model still fires at 0.8
    # rather than never firing.
    assert sdk_context_threshold_crossed(
        170_000, 200_000, "claude_code", "claude-sonnet-4-5") is True
    assert sdk_context_threshold_crossed(
        150_000, 200_000, "mystery", "nope") is False
    assert sdk_context_threshold_crossed(
        170_000, 200_000, "mystery", "nope") is True


def test_sdk_crossed_without_live_numbers_never_fires() -> None:
    from imp.providers.catalog import sdk_context_threshold_crossed
    assert sdk_context_threshold_crossed(
        170_000, None, "claude_code", "opus") is False
    assert sdk_context_threshold_crossed(
        None, 200_000, "claude_code", "opus") is False
    assert sdk_context_threshold_crossed(
        0, 200_000, "claude_code", "opus") is False
    assert sdk_context_threshold_crossed(
        170_000, 0, "claude_code", "opus") is False


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


def test_claude_code_catalog_has_opus_5_entries() -> None:
    # Both aliases present in the table (not merely resolved via the
    # conservative default), window-only like the existing opus/fable entries.
    assert "claude-opus-5" in CLAUDE_CODE
    assert "claude-opus-5[1m]" in CLAUDE_CODE
    assert CLAUDE_CODE["claude-opus-5"] == ModelInfo(window=200_000)
    assert CLAUDE_CODE["claude-opus-5[1m]"] == ModelInfo(window=1_000_000)


def test_anthropic_catalog_has_opus_5_entries() -> None:
    # Plain: 200k window, 128k output cap, no cli_model/betas indirection.
    assert "claude-opus-5" in ANTHROPIC
    assert ANTHROPIC["claude-opus-5"] == ModelInfo(
        window=200_000, max_output_tokens=128_000
    )
    # 1M: raw API rejects the bracketed form, so cli_model strips it and the
    # context-1m beta header requests the 1M window; 128k output cap.
    assert "claude-opus-5[1m]" in ANTHROPIC
    assert ANTHROPIC["claude-opus-5[1m]"] == ModelInfo(
        window=1_000_000,
        max_output_tokens=128_000,
        cli_model="claude-opus-5",
        betas=("context-1m-2025-08-07",),
    )


def test_limits_for_covers_fable() -> None:
    # The bracketed 1M form and the plain alias are both exact catalog keys.
    one_m = limits_for("anthropic", "claude-fable-5[1m]")
    plain = limits_for("claude_code", "claude-fable-5")
    assert one_m is not None and plain is not None
    assert (one_m.window, plain.window) == (1_000_000, 200_000)


def test_catalog_has_exactly_one_window_surface() -> None:
    # The substring map was a second window surface that duplicated the
    # per-alias `window`; limits_for is now the only lookup.
    import imp.providers.catalog as catalog
    for gone in ("_WINDOW_TOKENS", "window_for_model", "checkpoint_fraction"):
        assert not hasattr(catalog, gone)


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


def test_openai_5_6_family_carries_the_billing_soft_cap() -> None:
    # OpenAI bills a prompt above 272k at 2x input / 1.5x output for the WHOLE
    # request; 260k stays under it with headroom for the handoff turns. The bare
    # alias routes to Sol, so it needs the same cap (a `gpt-5` prefix fallback
    # would hand off at 320k, above the breakpoint).
    for alias in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6"):
        assert OPENAI[alias] == ModelInfo(
            window=1_050_000, max_output_tokens=128_000, soft_cap=260_000)


def test_soft_cap_is_absent_on_every_other_entry() -> None:
    # No claude model carries a billing breakpoint on this ladder.
    capped = {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6",
              "gpt-5.5", "gpt-5.4"}
    assert ModelInfo().soft_cap is None
    assert OPENAI["gpt-5"].soft_cap is None
    assert OPENAI["gpt-5.3-codex"].soft_cap is None
    # gpt-5.4-mini shares a prefix with the capped gpt-5.4 but has no
    # breakpoint of its own — exact keys, never prefixes.
    assert OPENAI["gpt-5.4-mini"].soft_cap is None
    assert all(info.soft_cap is None
               for key, info in OPENAI.items() if key not in capped)
    assert all(info.soft_cap is None for info in ANTHROPIC.values())
    assert all(info.soft_cap is None for info in CLAUDE_CODE.values())


def test_limits_for_resolves_exact_aliases_per_provider() -> None:
    assert limits_for("openai", "gpt-5.6-sol") == OPENAI["gpt-5.6-sol"]
    # codex shares the OPENAI table, so it inherits the cap with no entry of its own
    codex_sol = limits_for("codex", "gpt-5.6-sol")
    assert codex_sol is not None
    assert codex_sol.soft_cap == 260_000
    bare = limits_for("openai", "gpt-5.6")
    assert bare is not None
    assert (bare.window, bare.soft_cap) == (1_050_000, 260_000)
    anthropic_1m = limits_for("anthropic", "claude-opus-5[1m]")
    assert anthropic_1m is not None
    assert anthropic_1m.window == 1_000_000
    claude_code_1m = limits_for("claude_code", "claude-opus-5[1m]")
    assert claude_code_1m is not None
    assert claude_code_1m.window == 1_000_000
    claude_code_opus = limits_for("claude_code", "opus")
    assert claude_code_opus is not None
    assert claude_code_opus.window == 200_000


def test_limits_for_longest_prefix_covers_dated_ids() -> None:
    # A dated id keeps its base model's limits.
    anthropic_dated = limits_for("anthropic", "claude-opus-5-20260315")
    assert anthropic_dated is not None
    assert anthropic_dated.window == 200_000
    claude_code_dated = limits_for("claude_code", "claude-opus-5-20260315")
    assert claude_code_dated is not None
    assert claude_code_dated.window == 200_000


def test_limits_for_longest_prefix_beats_a_shorter_key() -> None:
    # Both "gpt-5.6-sol" and "gpt-5" are prefixes; the longest wins, so the
    # capped 5.6 entry answers rather than the uncapped 400k catch-all.
    info = limits_for("openai", "gpt-5.6-sol-2026-03")
    assert info is not None
    assert (info.window, info.soft_cap) == (1_050_000, 260_000)


def test_limits_for_unknown_provider_or_model_is_none() -> None:
    assert limits_for("mystery", "gpt-5.6-sol") is None
    assert limits_for("openai", "some-unlisted-model") is None
    assert limits_for("openai", None) is None
    assert limits_for(None, "gpt-5.6-sol") is None


def test_limits_for_is_case_sensitive() -> None:
    # Catalog keys are wire ids and model_info() is an exact dict lookup; folding
    # case here would make the two surfaces disagree about what is a known model.
    assert limits_for("openai", "GPT-5.6-Sol") is None
    assert limits_for("anthropic", "Opus") is None


def test_checkpoint_cutoff_caps_at_the_soft_cap() -> None:
    # 260k of a 1.05M window ~= 0.248 — far below the 0.8 default share.
    info = OPENAI["gpt-5.6-sol"]
    assert checkpoint_cutoff(info, info.window) == 260_000 / 1_050_000


def test_checkpoint_cutoff_without_a_soft_cap_is_the_default_fraction() -> None:
    assert DEFAULT_CHECKPOINT_FRACTION == 0.8
    assert checkpoint_cutoff(ANTHROPIC["claude-opus-5[1m]"], 1_000_000) == 0.8
    assert checkpoint_cutoff(None, 200_000) == 0.8


def test_checkpoint_cutoff_never_loosens_the_default_share() -> None:
    # A cap ABOVE 0.8 of the window must not push the handoff later.
    assert checkpoint_cutoff(
        ModelInfo(window=200_000, soft_cap=190_000), 200_000) == 0.8


def test_checkpoint_cutoff_without_a_window_is_none() -> None:
    assert checkpoint_cutoff(OPENAI["gpt-5.6-sol"], None) is None
    assert checkpoint_cutoff(OPENAI["gpt-5.6-sol"], 0) is None
    assert checkpoint_cutoff(OPENAI["gpt-5.6-sol"], -1) is None
    assert checkpoint_cutoff(None, None) is None


def test_context_tokens_from_usage_docstring_disclaims_live_occupancy() -> None:
    """The old wording — "the model's view of prompt size" — is what made a
    codex defect invisible: there a turn's usage is an element-wise sum over
    every model request in the turn, several times the real prompt."""
    # Collapse the docstring's wrapping so the assertions pin wording, not
    # where the lines happen to break.
    doc = " ".join((context_tokens_from_usage.__doc__ or "").split())
    assert "model's view of prompt size" not in doc
    assert "billing" in doc.lower()
    assert "single model request" in doc


def test_gpt_5_5_and_5_4_carry_the_1m_window_and_the_260k_soft_cap() -> None:
    for alias in ("gpt-5.5", "gpt-5.4"):
        info = OPENAI[alias]
        assert info.window == 1_050_000
        assert info.soft_cap == 260_000
        # 260k of a 1.05M window ~= 0.248 — the handoff lands far under the 0.8
        # default share, and so under the 272k billing breakpoint.
        assert checkpoint_cutoff(info, info.window) == 260_000 / 1_050_000
        assert checkpoint_cutoff(info, info.window) < DEFAULT_CHECKPOINT_FRACTION


def test_gpt_5_5_and_5_4_cross_the_threshold_at_the_soft_cap() -> None:
    from imp.providers.catalog import context_threshold_crossed
    for provider in ("openai", "codex"):
        for model in ("gpt-5.5", "gpt-5.4"):
            assert context_threshold_crossed(
                {"input_tokens": 261_000}, provider, model) is True
            assert context_threshold_crossed(
                {"input_tokens": 259_000}, provider, model) is False
            # deep past the old 320k cutoff, still far under 0.8 * 1.05M
            assert context_threshold_crossed(
                {"input_tokens": 700_000}, provider, model) is True


def test_the_uncapped_openai_models_keep_the_400k_window() -> None:
    for alias in ("gpt-5", "gpt-5.4-mini", "gpt-5.3-codex"):
        info = OPENAI[alias]
        assert info.window == 400_000
        assert info.soft_cap is None
