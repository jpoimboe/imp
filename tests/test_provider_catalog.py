from __future__ import annotations

from imp.providers.catalog import (
    ANTHROPIC,
    CLAUDE_CODE,
    DEFAULT_WINDOW,
    OPENAI,
    ModelInfo,
    checkpoint_fraction,
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
