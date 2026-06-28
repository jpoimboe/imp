from __future__ import annotations

from imp.providers.catalog import (
    ANTHROPIC,
    CLAUDE_CODE,
    DEFAULT_WINDOW,
    OPENAI,
    ModelInfo,
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
