from __future__ import annotations

import pytest

from imp.assets import default_workflow_dir
from imp.engine.loader import load_workflow
from imp.providers import resolve
from imp.providers.config import Config


class _FakeProvider:
    def __init__(self, name: str, model: str) -> None:
        self.name = name
        self.model = model

    def create_runner(self, invocation):
        raise NotImplementedError


_OPENAI = [True]


@pytest.fixture(autouse=True)
def _providers():
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "claude_code", env_check=lambda: True,
        factory=lambda m: _FakeProvider("claude_code", m))
    resolve.register_provider(
        "openai", env_check=lambda: _OPENAI[0],
        factory=lambda m: _FakeProvider("openai", m))
    yield
    resolve.reset_providers_for_tests()


def _config() -> Config:
    return Config(
        providers=("claude_code", "openai"),
        tiers={
            "low":    {"claude_code": "haiku",  "openai": "gpt-5.4-mini"},
            "medium": {"claude_code": "sonnet", "openai": "gpt-5.3-codex"},
            "high":   {"claude_code": "opus",   "openai": "gpt-5.5"},
        },
    )


def test_default_workflow_loads_and_resolves_review_steps() -> None:
    """Single provider by design: no review step declares differ_from, so the
    review steps inherit the medium tier (sonnet) and do NOT appear in
    resolved_steps (which holds only diverged steps)."""
    _OPENAI[0] = True
    wf = load_workflow(default_workflow_dir())
    resolved_tiers, resolved_steps, _details = resolve.resolve_run(wf, _config())
    assert resolved_tiers["medium"].provider == "claude_code"
    assert resolved_tiers["medium"].model == "sonnet"
    assert resolved_tiers["high"].provider == "claude_code"
    # No differ_from -> not diverged -> absent from resolved_steps (it holds only
    # steps that diverge from their tier via differ_from).
    assert "review" not in resolved_steps
    assert "integration-pass" not in resolved_steps


def test_review_steps_resolve_same_vendor_single_provider() -> None:
    """With only claude_code env-available, the medium tier still resolves to
    sonnet — no error, no cross-vendor requirement, no diverged review steps."""
    _OPENAI[0] = False
    wf = load_workflow(default_workflow_dir())
    resolved_tiers, resolved_steps, _details = resolve.resolve_run(wf, _config())
    assert resolved_tiers["medium"].provider == "claude_code"
    assert resolved_tiers["medium"].model == "sonnet"
    assert "review" not in resolved_steps
    assert "integration-pass" not in resolved_steps
