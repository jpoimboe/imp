from __future__ import annotations

from imp.providers.catalog import (
    ANTHROPIC,
    CLAUDE,
    DEFAULT_CHECKPOINT_CEILING,
    DEFAULT_CHECKPOINT_FRACTION,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_WINDOW,
    OPENAI,
    ModelInfo,
    catalog_aliases,
    context_tokens_from_usage,
    handoff_limit,
    limits_for,
    model_info,
    model_supports_vision,
)


def test_claude_catalog_opus_1m_entry() -> None:
    info = model_info(CLAUDE, "claude-opus-4-8[1m]")
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


def test_claude_catalog_opus_entry_is_plain_1m() -> None:
    info = model_info(CLAUDE, "opus")
    assert info.window == 1_000_000
    assert info.betas == ()
    assert info.cli_model is None


def test_unknown_alias_yields_conservative_default() -> None:
    info = model_info(CLAUDE, "claude-sonnet-4-5")
    assert info == ModelInfo(window=DEFAULT_WINDOW)
    assert info.betas == ()
    assert info.cli_model is None


def test_other_providers_seed_their_windows() -> None:
    assert model_info(ANTHROPIC, "opus").window == 1_000_000
    assert model_info(OPENAI, "gpt-5.5").window == 1_050_000


def test_context_threshold_crossed_fires_at_the_handoff_limit() -> None:
    from imp.providers.catalog import context_threshold_crossed
    # claude-sonnet-4-6's 200k window -> a 160k limit (80% binds below the 200k ceiling).
    assert context_threshold_crossed(
        {"input_tokens": 160_000}, "anthropic", "claude-sonnet-4-6") is True
    assert context_threshold_crossed(
        {"input_tokens": 170_000}, "anthropic", "claude-sonnet-4-6") is True


def test_context_threshold_crossed_not_below_the_limit() -> None:
    from imp.providers.catalog import context_threshold_crossed
    assert context_threshold_crossed(
        {"input_tokens": 159_999}, "anthropic", "claude-sonnet-4-6") is False
    assert context_threshold_crossed(
        {"input_tokens": 150_000}, "anthropic", "claude-sonnet-4-6") is False


def test_context_threshold_crossed_sums_cache_tokens() -> None:
    from imp.providers.catalog import context_threshold_crossed
    # 100000 + 60000 + 5000 = 165000 >= the 160k limit.
    assert context_threshold_crossed(
        {"input_tokens": 100_000, "cache_read_input_tokens": 60_000,
         "cache_creation_input_tokens": 5_000}, "anthropic", "claude-sonnet-4-6") is True


def test_context_threshold_crossed_unknown_model_or_provider_never_fires() -> None:
    from imp.providers.catalog import context_threshold_crossed
    assert context_threshold_crossed(
        {"input_tokens": 999_999}, "openai", "mystery-model") is False
    assert context_threshold_crossed(
        {"input_tokens": 999_999}, "mystery", "sonnet") is False


def test_context_threshold_crossed_without_usage_never_fires() -> None:
    from imp.providers.catalog import context_threshold_crossed
    assert context_threshold_crossed(None, "anthropic", "claude-sonnet-4-6") is False
    assert context_threshold_crossed({}, "anthropic", "claude-sonnet-4-6") is False


def test_gpt_5_6_crosses_at_the_ceiling_on_both_providers() -> None:
    from imp.providers.catalog import context_threshold_crossed
    # A 1.05M window is clamped by the 200k ceiling, not by 80%.
    for provider in ("openai", "codex"):
        for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6"):
            assert context_threshold_crossed(
                {"input_tokens": 199_999}, provider, model) is False
            assert context_threshold_crossed(
                {"input_tokens": 200_000}, provider, model) is True


def test_claude_opus_5_1m_crosses_at_the_ceiling_not_at_800k() -> None:
    from imp.providers.catalog import context_threshold_crossed
    # 200k of the 1M window: the 0.8 fraction would put this at 800k.
    for provider in ("anthropic", "claude"):
        assert context_threshold_crossed(
            {"input_tokens": 200_000}, provider, "claude-opus-5[1m]") is True
        assert context_threshold_crossed(
            {"input_tokens": 300_000}, provider, "claude-opus-5[1m]") is True
        assert context_threshold_crossed(
            {"input_tokens": 199_999}, provider, "claude-opus-5[1m]") is False


def test_sdk_crossed_fires_at_the_200k_ceiling_on_a_big_window() -> None:
    from imp.providers.catalog import sdk_context_threshold_crossed
    assert sdk_context_threshold_crossed(199_999, 1_050_000) is False
    assert sdk_context_threshold_crossed(200_000, 1_050_000) is True
    # The claude path on a live 1M window.
    assert sdk_context_threshold_crossed(200_000, 1_000_000) is True
    assert sdk_context_threshold_crossed(199_999, 1_000_000) is False


def test_sdk_crossed_on_a_200k_window_fires_at_eighty_percent() -> None:
    from imp.providers.catalog import sdk_context_threshold_crossed
    assert sdk_context_threshold_crossed(160_000, 200_000) is True
    assert sdk_context_threshold_crossed(170_000, 200_000) is True
    assert sdk_context_threshold_crossed(159_999, 200_000) is False


def test_sdk_crossed_without_live_numbers_never_fires() -> None:
    from imp.providers.catalog import sdk_context_threshold_crossed
    assert sdk_context_threshold_crossed(170_000, None) is False
    assert sdk_context_threshold_crossed(None, 200_000) is False
    assert sdk_context_threshold_crossed(0, 200_000) is False
    assert sdk_context_threshold_crossed(170_000, 0) is False


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
    # 4.x: opus 64000, sonnet/haiku 32000; the bare aliases follow 5.5's 128000.
    assert model_info(ANTHROPIC, "claude-opus-4-8").max_output_tokens == 64_000
    assert model_info(ANTHROPIC, "claude-sonnet-4-6").max_output_tokens == 32_000
    assert model_info(ANTHROPIC, "claude-haiku-4-5").max_output_tokens == 32_000
    for alias in ("opus", "sonnet", "haiku"):
        assert model_info(ANTHROPIC, alias).max_output_tokens == 128_000, alias


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
    assert model_supports_vision("claude", "sonnet") is True
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


def test_claude_catalog_has_fable_entries() -> None:
    # Both aliases are present in the table (not merely resolved via the
    # conservative default), window-only like the existing opus entries.
    assert "claude-fable-5" in CLAUDE
    assert "claude-fable-5[1m]" in CLAUDE
    assert CLAUDE["claude-fable-5"] == ModelInfo(window=1_000_000)
    assert CLAUDE["claude-fable-5[1m]"] == ModelInfo(window=1_000_000)


def test_anthropic_catalog_has_fable_entries() -> None:
    # Plain: native 1M window, 128k output cap, no cli_model/betas indirection.
    assert "claude-fable-5" in ANTHROPIC
    assert ANTHROPIC["claude-fable-5"] == ModelInfo(
        window=1_000_000, max_output_tokens=128_000
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


def test_claude_catalog_has_opus_5_entries() -> None:
    # Both aliases present in the table (not merely resolved via the
    # conservative default), window-only like the existing opus/fable entries.
    assert "claude-opus-5" in CLAUDE
    assert "claude-opus-5[1m]" in CLAUDE
    assert CLAUDE["claude-opus-5"] == ModelInfo(window=1_000_000)
    assert CLAUDE["claude-opus-5[1m]"] == ModelInfo(window=1_000_000)


def test_anthropic_catalog_has_opus_5_entries() -> None:
    # Plain: native 1M window, 128k output cap, no cli_model/betas indirection.
    assert "claude-opus-5" in ANTHROPIC
    assert ANTHROPIC["claude-opus-5"] == ModelInfo(
        window=1_000_000, max_output_tokens=128_000
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


def test_claude_catalog_has_opus_5_5_entries() -> None:
    # Both aliases present in the table (not merely resolved via the
    # conservative default), window-only like the opus-5 entries above.
    assert "claude-opus-5-5" in CLAUDE
    assert "claude-opus-5-5[1m]" in CLAUDE
    assert CLAUDE["claude-opus-5-5"] == ModelInfo(window=1_000_000, adaptive_only=True)
    assert CLAUDE["claude-opus-5-5[1m]"] == ModelInfo(window=1_000_000, adaptive_only=True)


def test_anthropic_catalog_has_opus_5_5_entries() -> None:
    # Plain: native 1M window, 128k output cap, no cli_model/betas indirection.
    assert "claude-opus-5-5" in ANTHROPIC
    assert ANTHROPIC["claude-opus-5-5"] == ModelInfo(
        window=1_000_000, max_output_tokens=128_000, adaptive_only=True
    )
    # 1M: the raw API rejects the bracketed form, so cli_model strips it — but
    # 5.5's 1M window is native, so no context-1m beta rides along.
    assert "claude-opus-5-5[1m]" in ANTHROPIC
    assert ANTHROPIC["claude-opus-5-5[1m]"] == ModelInfo(
        window=1_000_000,
        max_output_tokens=128_000,
        cli_model="claude-opus-5-5",
        adaptive_only=True,
    )
    assert ANTHROPIC["claude-opus-5-5[1m]"].betas == ()
    # The same values through the resolver an agent run actually calls.
    resolved = limits_for("anthropic", "claude-opus-5-5[1m]")
    assert resolved is not None
    assert (resolved.cli_model, resolved.betas) == ("claude-opus-5-5", ())


def test_opus_5_5_is_listed_ahead_of_opus_5_in_both_rosters() -> None:
    # catalog_aliases returns table order and /model prints it, so placement IS
    # display order: 5.5 leads the family it supersedes, on both providers.
    for provider in ("claude", "anthropic"):
        aliases = catalog_aliases(provider)
        last_5_5 = max(aliases.index(key) for key in
                       ("claude-opus-5-5", "claude-opus-5-5[1m]"))
        first_5 = min(aliases.index(key) for key in
                      ("claude-opus-5", "claude-opus-5[1m]"))
        assert last_5_5 < first_5, provider


def test_limits_for_covers_fable() -> None:
    # Both forms are exact catalog keys — a prefix fallback would miss entirely,
    # not land on 1M — and both now carry the native 1M window.
    one_m = limits_for("anthropic", "claude-fable-5[1m]")
    plain = limits_for("anthropic", "claude-fable-5")
    claude_plain = limits_for("claude", "claude-fable-5")
    assert one_m is not None and plain is not None and claude_plain is not None
    assert (one_m.window, plain.window, claude_plain.window) == (
        1_000_000, 1_000_000, 1_000_000)
    # Only the bracketed form is rejected by the raw API, so only it carries the
    # cli_model strip and the context-1m opt-in.
    assert (one_m.cli_model, one_m.betas) == (
        "claude-fable-5", ("context-1m-2025-08-07",))
    assert (plain.cli_model, plain.betas) == (None, ())


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
    assert "sonnet" in catalog_aliases("claude")
    # codex shares the OPENAI table
    assert catalog_aliases("codex") == catalog_aliases("openai")
    assert "gpt-5" in catalog_aliases("openai")


def test_catalog_aliases_unknown_provider_is_empty() -> None:
    assert catalog_aliases("nope") == ()


def test_legacy_openai_models_use_the_default_ceiling() -> None:
    for alias in (
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
        "gpt-5.6", "gpt-5.5", "gpt-5.4",
    ):
        info = OPENAI[alias]
        assert info.window == 1_050_000
        assert info.max_output_tokens == 128_000
        assert handoff_limit(info.window) == 200_000


def test_astra_catalog_and_dated_limits_are_shared_by_openai_and_codex() -> None:
    expected = ModelInfo(window=1_050_000, max_output_tokens=128_000)
    assert OPENAI["gpt-6-astra"] == expected
    for provider in ("openai", "codex"):
        assert limits_for(provider, "gpt-6-astra") == expected
        assert limits_for(provider, "gpt-6-astra-2026-09-10") == expected
        assert "gpt-6-astra" in catalog_aliases(provider)


def test_astra_hands_off_at_200k_like_every_other_big_window() -> None:
    from imp.providers.catalog import context_threshold_crossed

    info = OPENAI["gpt-6-astra"]
    assert handoff_limit(info.window) == 200_000
    assert context_threshold_crossed(
        {"input_tokens": 199_999}, "openai", "gpt-6-astra") is False
    assert context_threshold_crossed(
        {"input_tokens": 200_000}, "openai", "gpt-6-astra") is True


def test_the_1m_anthropic_models_all_hand_off_at_the_200k_ceiling() -> None:
    # 0.8 * 1M is 800k, but the absolute ceiling binds first: every one of these
    # hands off at exactly 200k, on either provider.
    for provider in ("anthropic", "claude"):
        for model in ("claude-opus-5-5", "claude-opus-5", "claude-fable-5",
                      "claude-opus-4-8"):
            info = limits_for(provider, model)
            assert info is not None, (provider, model)
            assert info.window == 1_000_000, (provider, model)
            assert handoff_limit(info.window) == 200_000, (provider, model)


def test_the_soft_cap_field_and_checkpoint_cutoff_are_gone() -> None:
    import imp.providers.catalog as cat
    assert "soft_cap" not in ModelInfo.__dataclass_fields__
    assert not hasattr(cat, "checkpoint_cutoff")


def test_limits_for_resolves_exact_aliases_per_provider() -> None:
    assert limits_for("openai", "gpt-5.6-sol") == OPENAI["gpt-5.6-sol"]
    codex_sol = limits_for("codex", "gpt-5.6-sol")
    assert codex_sol is not None
    assert codex_sol.window == 1_050_000
    bare = limits_for("openai", "gpt-5.6")
    assert bare is not None
    assert bare.window == 1_050_000
    anthropic_1m = limits_for("anthropic", "claude-opus-5[1m]")
    assert anthropic_1m is not None
    assert anthropic_1m.window == 1_000_000
    claude_1m = limits_for("claude", "claude-opus-5[1m]")
    assert claude_1m is not None
    assert claude_1m.window == 1_000_000
    claude_opus = limits_for("claude", "opus")
    assert claude_opus is not None
    assert claude_opus.window == 1_000_000


def test_limits_for_longest_prefix_covers_dated_ids() -> None:
    # A dated id keeps its base model's limits — and never collapses onto the
    # newer `claude-opus-5-5` key, which it does not start with.
    anthropic_dated = limits_for("anthropic", "claude-opus-5-20260315")
    assert anthropic_dated is ANTHROPIC["claude-opus-5"]
    assert anthropic_dated.window == 1_000_000
    claude_dated = limits_for("claude", "claude-opus-5-20260315")
    assert claude_dated is CLAUDE["claude-opus-5"]
    assert claude_dated.window == 1_000_000


def test_limits_for_longest_prefix_beats_a_shorter_key() -> None:
    # Both "gpt-5.6-sol" and "gpt-5" are prefixes; the longest wins.
    info = limits_for("openai", "gpt-5.6-sol-2026-03")
    assert info is not None
    assert info.window == 1_050_000


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


def test_default_checkpoint_ceiling_is_200000() -> None:
    assert DEFAULT_CHECKPOINT_CEILING == 200_000


def test_handoff_limit_is_the_ceiling_or_eighty_percent_of_the_window() -> None:
    assert DEFAULT_CHECKPOINT_FRACTION == 0.8
    # The fraction binds while it is the lower of the two — at or below a 250k
    # window; above that the absolute ceiling takes over.
    assert handoff_limit(200_000) == 160_000
    assert handoff_limit(220_000) == 176_000
    assert handoff_limit(400_000) == 200_000
    assert handoff_limit(1_000_000) == 200_000
    assert handoff_limit(1_050_000) == 200_000


def test_handoff_limit_without_a_usable_window_is_none() -> None:
    assert handoff_limit(None) is None
    assert handoff_limit(0) is None
    assert handoff_limit(-1) is None


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


def test_gpt_5_5_and_5_4_cross_the_threshold_at_the_ceiling() -> None:
    from imp.providers.catalog import context_threshold_crossed
    for provider in ("openai", "codex"):
        for model in ("gpt-5.5", "gpt-5.4"):
            assert context_threshold_crossed(
                {"input_tokens": 199_999}, provider, model) is False
            assert context_threshold_crossed(
                {"input_tokens": 200_000}, provider, model) is True


def test_the_400k_window_models_hand_off_at_the_ceiling() -> None:
    for alias in ("gpt-5", "gpt-5.4-mini", "gpt-5.3-codex"):
        info = OPENAI[alias]
        assert info.window == 400_000
        assert handoff_limit(info.window) == 200_000


_ADAPTIVE_ONLY = {
    "claude-opus-5-5", "claude-opus-5-5[1m]", "claude-sonnet-5-5",
    "claude-sonnet-5-5[1m]", "claude-haiku-5-5", "opus", "sonnet", "haiku",
}


def test_adaptive_only_is_exactly_the_5_5_generation_and_the_bare_aliases() -> None:
    for catalog in (CLAUDE, ANTHROPIC):
        assert {k for k, v in catalog.items() if v.adaptive_only} == _ADAPTIVE_ONLY
    assert not any(v.adaptive_only for v in OPENAI.values())
    assert ModelInfo().adaptive_only is False
    for provider in ("claude", "anthropic"):
        assert limits_for(provider, "claude-haiku-5-5").adaptive_only is True
        assert limits_for(provider, "claude-haiku-5-5-20261007").adaptive_only is True
        assert limits_for(provider, "claude-haiku-4-5").adaptive_only is False
        assert limits_for(provider, "claude-sonnet-4-6").adaptive_only is False


def test_claude_catalog_has_sonnet_and_haiku_5_5_entries() -> None:
    expected = ModelInfo(window=1_000_000, adaptive_only=True)
    for key in ("claude-sonnet-5-5", "claude-sonnet-5-5[1m]", "claude-haiku-5-5",
                "haiku", "sonnet", "opus"):
        assert key in CLAUDE, key
        assert limits_for("claude", key) == expected, key
    # Haiku 5.5 has no [1m] form.
    assert "claude-haiku-5-5[1m]" not in CLAUDE
    # The 4.x entries stay as they were.
    assert CLAUDE["claude-haiku-4-5"] == ModelInfo(window=200_000)
    assert CLAUDE["claude-sonnet-4-6"] == ModelInfo(window=200_000)


def test_anthropic_catalog_has_sonnet_and_haiku_5_5_entries() -> None:
    expected = ModelInfo(window=1_000_000, max_output_tokens=128_000, adaptive_only=True)
    for key in ("claude-sonnet-5-5", "claude-haiku-5-5", "haiku", "sonnet", "opus"):
        assert key in ANTHROPIC, key
        assert limits_for("anthropic", key) == expected, key
    # The raw API rejects the bracketed form: cli_model strips it; 1M is native, so no beta.
    assert limits_for("anthropic", "claude-sonnet-5-5[1m]") == ModelInfo(
        window=1_000_000, max_output_tokens=128_000,
        cli_model="claude-sonnet-5-5", adaptive_only=True)
    assert limits_for("anthropic", "claude-sonnet-5-5[1m]").cli_model == "claude-sonnet-5-5"
    assert "claude-haiku-5-5[1m]" not in ANTHROPIC
    assert ANTHROPIC["claude-haiku-4-5"] == ModelInfo(window=200_000, max_output_tokens=32_000)
    assert ANTHROPIC["claude-sonnet-4-6"] == ModelInfo(window=200_000, max_output_tokens=32_000)
