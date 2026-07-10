from types import SimpleNamespace

from imp.cli.model_command import validate_model_arg


def test_validate_rejects_malformed_syntax() -> None:
    err = validate_model_arg("anthropic:", change=SimpleNamespace())
    assert err is not None and "anthropic:" in err


def test_validate_rejects_unavailable_provider(monkeypatch) -> None:
    import imp.cli.model_command as mod
    monkeypatch.setattr(mod, "_available_for_change", lambda change: ["anthropic"])
    err = validate_model_arg("openai:gpt-5", change=SimpleNamespace())
    assert err is not None and "openai" in err


def test_validate_accepts_available_provider(monkeypatch) -> None:
    import imp.cli.model_command as mod
    monkeypatch.setattr(mod, "_available_for_change", lambda change: ["anthropic"])
    assert validate_model_arg(
        "anthropic:claude-fable-5", change=SimpleNamespace()) is None


def test_validate_bare_model_needs_no_provider_check(monkeypatch) -> None:
    import imp.cli.model_command as mod
    monkeypatch.setattr(mod, "_available_for_change", lambda change: [])
    assert validate_model_arg("claude-fable-5", change=SimpleNamespace()) is None
