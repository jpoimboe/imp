# tests/test_resolve_roles.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.errors import LoadError
from imp.engine.types import (
    AgentDef,
    OutputField,
    OutputSchema,
    Pause,
    Phase,
    RoleConstraint,
    SourceLoc,
    Step,
    Vars,
    Workflow,
    WorkflowDefaults,
)
from imp.providers import resolve
from imp.providers.config import Config, ProviderModel, RoleConfig
from imp.providers.resolve import ProviderUnavailable, resolve_roles

_SRC = SourceLoc(file=Path("w.yaml"), line=1)


class _FakeProvider:
    def __init__(self, name: str, model: str) -> None:
        self.name = name
        self.model = model

    def create_runner(self, invocation):
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _providers(monkeypatch):
    resolve.reset_providers_for_tests()
    # anthropic always present; openai presence toggled per-test via env flag.
    resolve.register_provider(
        "anthropic", env_check=lambda: True,
        factory=lambda m: _FakeProvider("anthropic", m))
    resolve.register_provider(
        "openai", env_check=lambda: _OPENAI_PRESENT[0],
        factory=lambda m: _FakeProvider("openai", m))
    yield
    resolve.reset_providers_for_tests()


_OPENAI_PRESENT = [True]  # mutable flag the env_check closure reads


def _agent(name: str, model_role: str | None = None, subagents=()) -> AgentDef:
    return AgentDef(
        name=name, description="", tools=frozenset(), subagents=tuple(subagents),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error")),), source=_SRC),
        prompt_template_source="", source_path=Path(f"{name}.md"), source=_SRC,
        model_role=model_role)


def _step(name: str, role: str, file_stem: str) -> Step:
    return Step(
        name=name, phase="p", kind="agent", rules=(), options=(),
        on_max_retries=Pause(), max_loops=6, source=_SRC,
        file=Path(f"agents/{file_stem}.md"), model_role=role)


def _wf(steps, agents, model_rules=None) -> Workflow:
    return Workflow(
        name="w", description="", phases=(Phase(name="p", steps=tuple(steps),
                                                source=_SRC),),
        vars=Vars(decls=()),
        defaults=WorkflowDefaults(max_retries=0, on_max_retries=Pause()),
        agents=agents, workflow_dir=Path("."), source=_SRC,
        model_rules=model_rules or {})


def _cfg(roles, model_rules=None) -> Config:
    return Config(roles=roles, source=_SRC, model_rules=model_rules or {})


def _table_config():
    return _cfg({
        "code": RoleConfig(name="code", prefer=(
            ProviderModel("anthropic", "haiku"),
            ProviderModel("openai", "gpt-5-mini"))),
        "code_review": RoleConfig(name="code_review", prefer=(
            ProviderModel("anthropic", "sonnet"),     # precedes the other-vendor entry
            ProviderModel("openai", "gpt-5.2"))),
    })


def _table_workflow():
    agents = {"coder": _agent("coder"), "reviewer": _agent("reviewer")}
    steps = [_step("coder", "code", "coder"),
             _step("reviewer", "code_review", "reviewer")]
    return _wf(steps, agents,
               model_rules={"code_review": RoleConstraint(differ_provider_from="code")})


def test_no_constraint_matches_resolve_role():
    _OPENAI_PRESENT[0] = True
    cfg = _cfg({"code": RoleConfig(name="code",
                                   prefer=(ProviderModel("anthropic", "haiku"),))})
    wf = _wf([_step("coder", "code", "coder")], {"coder": _agent("coder")})
    resolved, details = resolve_roles(wf, cfg)
    assert resolved["code"] == ProviderModel("anthropic", "haiku")
    assert details["code"]["constraint"] is None
    assert details["code"]["differ_tier"] is None


def test_table_two_vendors_jumps_vendor():
    _OPENAI_PRESENT[0] = True
    resolved, details = resolve_roles(_table_workflow(), _table_config())
    assert resolved["code"] == ProviderModel("anthropic", "haiku")
    assert resolved["code_review"] == ProviderModel("openai", "gpt-5.2")  # tier 1
    assert details["code_review"]["differ_tier"] == 1
    assert details["code_review"]["constraint"] == "differ_provider_from"


def test_table_one_vendor_falls_back_same_vendor_model():
    _OPENAI_PRESENT[0] = False  # only anthropic available
    resolved, details = resolve_roles(_table_workflow(), _table_config())
    assert resolved["code"] == ProviderModel("anthropic", "haiku")
    assert resolved["code_review"] == ProviderModel("anthropic", "sonnet")  # tier 2
    assert details["code_review"]["differ_tier"] == 2


def test_config_model_rules_override_workflow():
    _OPENAI_PRESENT[0] = True
    wf = _table_workflow()  # workflow says code_review differs from code
    # config overrides code_review to have NO constraint → first available wins
    cfg = _table_config()
    cfg = _cfg(cfg.roles, model_rules={"code_review": RoleConstraint(
        differ_provider_from=None)})
    resolved, _ = resolve_roles(wf, cfg)
    # no constraint → first env-available prefer entry = anthropic/sonnet
    assert resolved["code_review"] == ProviderModel("anthropic", "sonnet")


def test_unconfigured_role_raises():
    _OPENAI_PRESENT[0] = True
    wf = _wf([_step("coder", "ghost", "coder")], {"coder": _agent("coder")})
    cfg = _cfg({"default": RoleConfig(name="default",
                                      prefer=(ProviderModel("anthropic", "m"),))})
    with pytest.raises(LoadError, match="not configured"):
        resolve_roles(wf, cfg)


def test_self_reference_cycle_raises():
    _OPENAI_PRESENT[0] = True
    wf = _wf([_step("coder", "code", "coder")], {"coder": _agent("coder")},
             model_rules={"code": RoleConstraint(differ_provider_from="code")})
    cfg = _cfg({"code": RoleConfig(name="code",
                                   prefer=(ProviderModel("anthropic", "m"),))})
    with pytest.raises(LoadError, match="cycle"):
        resolve_roles(wf, cfg)


def test_empty_after_env_filter_raises_provider_unavailable():
    _OPENAI_PRESENT[0] = False
    # code_review prefers ONLY openai, which is absent
    cfg = _cfg({
        "code": RoleConfig(name="code", prefer=(ProviderModel("anthropic", "haiku"),)),
        "code_review": RoleConfig(name="code_review",
                                  prefer=(ProviderModel("openai", "gpt-5.2"),)),
    })
    wf = _wf([_step("coder", "code", "coder"),
              _step("reviewer", "code_review", "reviewer")],
             {"coder": _agent("coder"), "reviewer": _agent("reviewer")},
             model_rules={"code_review": RoleConstraint(differ_provider_from="code")})
    with pytest.raises(ProviderUnavailable):
        resolve_roles(wf, cfg)


def test_subagent_role_is_collected():
    _OPENAI_PRESENT[0] = True
    # parent step uses 'code'; its subagent 'reviewer' declares model_role code_review
    agents = {
        "coder": _agent("coder", subagents=("reviewer",)),
        "reviewer": _agent("reviewer", model_role="code_review"),
    }
    wf = _wf([_step("coder", "code", "coder")], agents,
             model_rules={"code_review": RoleConstraint(differ_provider_from="code")})
    resolved, _ = resolve_roles(wf, _table_config())
    assert "code_review" in resolved
    assert resolved["code_review"].provider == "openai"
