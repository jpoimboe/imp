import pytest

from imp.providers.model_arg import ModelArgError, ParsedModelArg, parse_model_arg


def test_parse_provider_and_model() -> None:
    assert parse_model_arg("anthropic:claude-fable-5") == ParsedModelArg(
        provider="anthropic", model="claude-fable-5")


def test_parse_bare_model_keeps_provider_none() -> None:
    assert parse_model_arg("claude-fable-5") == ParsedModelArg(
        provider=None, model="claude-fable-5")


def test_parse_strips_surrounding_whitespace() -> None:
    assert parse_model_arg("  anthropic : claude-fable-5 ") == ParsedModelArg(
        provider="anthropic", model="claude-fable-5")


@pytest.mark.parametrize("bad", ["", "   ", "anthropic:", ":claude-fable-5", ":"])
def test_parse_rejects_malformed(bad: str) -> None:
    with pytest.raises(ModelArgError):
        parse_model_arg(bad)


def test_parse_bare_model_strips_whitespace() -> None:
    assert parse_model_arg("  claude-fable-5  ") == ParsedModelArg(
        provider=None, model="claude-fable-5")
