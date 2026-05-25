"""Provider resolution: prefer: list → BaseChatModel."""

from __future__ import annotations

import pytest
from langchain_core.language_models import BaseChatModel

from imp.providers.config import ProviderModel, RoleConfig
from imp.providers.resolve import (
    ProviderUnavailable,
    UnknownProvider,
    resolve_role,
)


def _role(*entries: tuple[str, str]) -> RoleConfig:
    return RoleConfig(
        name="test",
        prefer=tuple(ProviderModel(provider=p, model=m) for p, m in entries),
    )


def test_anthropic_resolves_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    model = resolve_role(_role(("anthropic", "claude-sonnet-4-6")))
    assert isinstance(model, BaseChatModel)
    assert model.__class__.__name__ == "ChatAnthropic"


def test_unknown_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with pytest.raises(UnknownProvider) as exc:
        resolve_role(_role(("zzz", "x")))
    assert "zzz" in str(exc.value)


def test_missing_env_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ProviderUnavailable) as exc:
        resolve_role(_role(("anthropic", "claude-sonnet-4-6")))
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_first_entry_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    model = resolve_role(
        _role(
            ("anthropic", "claude-opus-4-7"),
            ("anthropic", "claude-sonnet-4-6"),
        )
    )
    # First listed entry wins when available
    assert getattr(model, "model", "") == "claude-opus-4-7"
