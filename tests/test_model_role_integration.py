# tests/test_model_role_integration.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.assets import default_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.model_rules import parse_role_constraints
from imp.engine.types import SourceLoc
from imp.providers import resolve
from imp.providers.config import Config, ProviderModel, RoleConfig

_SRC = SourceLoc(file=Path("c.yaml"), line=1)


class _FakeProvider:
    def __init__(self, name: str, model: str) -> None:
        self.name = name
        self.model = model

    def create_runner(self, invocation: object) -> object:
        raise NotImplementedError


_OPENAI = [True]


@pytest.fixture(autouse=True)
def _providers():
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "anthropic", env_check=lambda: True,
        factory=lambda m: _FakeProvider("anthropic", m))
    resolve.register_provider(
        "openai", env_check=lambda: _OPENAI[0],
        factory=lambda m: _FakeProvider("openai", m))
    resolve.register_provider(
        "claude_code", env_check=lambda: False,
        factory=lambda m: _FakeProvider("claude_code", m))
    yield
    resolve.reset_providers_for_tests()


def _two_vendor_config() -> Config:
    # Must list EVERY role the bundled workflow references — resolve_roles raises
    # if a collected role is absent from config.  The real bundled workflow uses:
    #   spec (spec-writer), spec_review (spec-reviewer), default (plan-writer),
    #   code (coder + documenter), code_review (code-reviewer).
    # No subagents declare model_role, so these five cover the full set.
    def role(name: str, prefer: list[tuple[str, str]]) -> RoleConfig:
        return RoleConfig(name=name, prefer=tuple(ProviderModel(*p) for p in prefer))

    roles: dict[str, RoleConfig] = {
        "default": role("default", [("anthropic", "claude-opus-4-7")]),
        "spec": role("spec", [("anthropic", "claude-opus-4-7")]),
        "spec_review": role("spec_review", [("anthropic", "claude-opus-4-7")]),
        "code": role("code", [("anthropic", "claude-haiku-4-5-20251001"),
                               ("openai", "gpt-5-mini")]),
        "code_review": role("code_review", [("anthropic", "claude-sonnet-4-6"),
                                             ("openai", "gpt-5.2")]),
    }
    return Config(roles=roles, source=_SRC,
                  model_rules=parse_role_constraints(None, Path("c.yaml")))


def test_default_workflow_two_vendor_diversity() -> None:
    _OPENAI[0] = True
    wf = load_workflow(default_workflow_dir())
    resolved, details = resolve.resolve_roles(wf, _two_vendor_config())
    assert resolved["code"] == ProviderModel("anthropic", "claude-haiku-4-5-20251001")
    assert resolved["code_review"] == ProviderModel("openai", "gpt-5.2")
    assert resolved["spec"].model == "claude-opus-4-7"
    assert details["code_review"]["differ_tier"] == 1


def test_default_workflow_one_vendor_falls_back_to_sonnet() -> None:
    _OPENAI[0] = False
    wf = load_workflow(default_workflow_dir())
    resolved, details = resolve.resolve_roles(wf, _two_vendor_config())
    assert resolved["code"] == ProviderModel("anthropic", "claude-haiku-4-5-20251001")
    assert resolved["code_review"] == ProviderModel("anthropic", "claude-sonnet-4-6")
    assert details["code_review"]["differ_tier"] == 2
