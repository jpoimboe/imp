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


def test_default_workflow_loads_and_resolves_two_vendor() -> None:
    """spec acceptance #2: with both vendors env-available, the shipped
    default workflow loads + resolves; code-reviewer's resolved provider
    differs from task-lead's."""
    _OPENAI[0] = True
    wf = load_workflow(default_workflow_dir())
    resolved_tiers, resolved_steps, _details = resolve.resolve_run(wf, _config())
    assert resolved_tiers["medium"].provider == "claude_code"
    assert resolved_tiers["medium"].model == "sonnet"
    assert resolved_tiers["high"].provider == "claude_code"
    # code-reviewer (differ_from: task-lead) jumps to openai.
    assert resolved_steps["code-reviewer"].provider == "openai"
    assert resolved_steps["code-reviewer"].model == "gpt-5.5"


def test_default_workflow_one_vendor_falls_back_same_vendor() -> None:
    """spec Decision §9: single-provider install, code-reviewer falls back
    to same vendor as task-lead (no error)."""
    _OPENAI[0] = False
    wf = load_workflow(default_workflow_dir())
    resolved_tiers, resolved_steps, _details = resolve.resolve_run(wf, _config())
    assert resolved_tiers["medium"].provider == "claude_code"
    # code-reviewer stays on claude_code; the high tier's model is opus, while
    # task-lead resolved to sonnet (medium) — same vendor, different model.
    assert resolved_steps["code-reviewer"].provider == "claude_code"
    assert resolved_steps["code-reviewer"].model == "opus"
