# tests/test_resolve.py
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
    SourceLoc,
    Step,
    Vars,
    Workflow,
    WorkflowDefaults,
)
from imp.providers import resolve
from imp.providers.config import Config, ProviderModel
from imp.providers.resolve import ProviderUnavailable, resolve_run

_SRC = SourceLoc(file=Path("w.yaml"), line=1)
_OPENAI_PRESENT = [True]


class _FakeProvider:
    def __init__(self, name: str, model: str) -> None:
        self.name = name
        self.model = model

    def create_runner(self, invocation):
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _providers():
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "claude_code", env_check=lambda: True,
        factory=lambda m: _FakeProvider("claude_code", m))
    resolve.register_provider(
        "openai", env_check=lambda: _OPENAI_PRESENT[0],
        factory=lambda m: _FakeProvider("openai", m))
    yield
    resolve.reset_providers_for_tests()


def _agent(name: str, tier: str | None = None, subagents=()) -> AgentDef:
    return AgentDef(
        name=name, description="", tools=frozenset(),
        subagents=tuple(subagents),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error")),), source=_SRC),
        prompt_template_source="", source_path=Path(f"{name}.md"), source=_SRC,
        tier=tier)


def _step(name: str, tier: str, file_stem: str,
          differ_from: str | None = None) -> Step:
    return Step(
        name=name, phase="p", kind="agent", rules=(), options=(),
        on_max_retries=Pause(), max_loops=6, source=_SRC,
        file=Path(f"agents/{file_stem}.md"), tier=tier, differ_from=differ_from)


def _wf(steps, agents) -> Workflow:
    return Workflow(
        name="w", description="",
        phases=(Phase(name="p", steps=tuple(steps), source=_SRC),),
        vars=Vars(decls=()),
        defaults=WorkflowDefaults(max_retries=0, on_max_retries=Pause()),
        agents=agents, workflow_dir=Path("."), source=_SRC)


def _cfg(providers=("claude_code", "openai")) -> Config:
    return Config(
        providers=tuple(providers),
        tiers={
            "low":    {"claude_code": "haiku",  "openai": "gpt-5.4-mini"},
            "medium": {"claude_code": "sonnet", "openai": "gpt-5.3-codex"},
            "high":   {"claude_code": "opus",   "openai": "gpt-5.5"},
        },
    )


def test_tier_map_first_available_wins_with_both_envs() -> None:
    _OPENAI_PRESENT[0] = True
    wf = _wf([_step("s1", "medium", "writer")], {"writer": _agent("writer")})
    tiers, steps, details = resolve_run(wf, _cfg())
    assert tiers["medium"] == ProviderModel("claude_code", "sonnet")
    assert steps == {}
    assert details["tiers"]["medium"]["provider"] == "claude_code"


def test_tier_map_falls_back_when_first_unavailable() -> None:
    _OPENAI_PRESENT[0] = True
    wf = _wf([_step("s1", "medium", "writer")], {"writer": _agent("writer")})
    # Reverse preference: openai first.
    tiers, _steps, _details = resolve_run(wf, _cfg(providers=("openai", "claude_code")))
    assert tiers["medium"] == ProviderModel("openai", "gpt-5.3-codex")


def test_differ_from_flips_vendor_when_both_available() -> None:
    """spec acceptance #5: differ_from picks the vendor that differs from
    the target's resolved (provider, model)."""
    _OPENAI_PRESENT[0] = True
    wf = _wf(
        [_step("s1", "high", "a"),
         _step("s2", "high", "b", differ_from="s1")],
        {"a": _agent("a"), "b": _agent("b")},
    )
    tiers, steps, details = resolve_run(wf, _cfg())
    assert tiers["high"] == ProviderModel("claude_code", "opus")
    assert steps["s2"] == ProviderModel("openai", "gpt-5.5")
    assert details["steps"]["s2"]["differ_tier"] == 1
    assert details["steps"]["s2"]["differ_from"] == "s1"


def test_differ_from_single_provider_falls_back_same_vendor_model() -> None:
    """spec Decision §9: single-provider install, differ_from gracefully
    falls back to same-vendor (no error)."""
    _OPENAI_PRESENT[0] = False  # only claude_code available
    wf = _wf(
        [_step("s1", "high", "a"),
         _step("s2", "high", "b", differ_from="s1")],
        {"a": _agent("a"), "b": _agent("b")},
    )
    _tiers, steps, details = resolve_run(wf, _cfg())
    # s2 falls back to claude_code/opus (same vendor, same model -> tier 3).
    assert steps["s2"].provider == "claude_code"
    assert details["steps"]["s2"]["differ_tier"] == 3


def test_differ_from_unknown_target_raises_loaderror() -> None:
    _OPENAI_PRESENT[0] = True
    wf = _wf(
        [_step("s1", "high", "a", differ_from="ghost")],
        {"a": _agent("a")},
    )
    with pytest.raises(LoadError, match="ghost"):
        resolve_run(wf, _cfg())


def test_differ_from_self_reference_raises_loaderror() -> None:
    _OPENAI_PRESENT[0] = True
    wf = _wf(
        [_step("s1", "high", "a", differ_from="s1")],
        {"a": _agent("a")},
    )
    with pytest.raises(LoadError, match=r"cannot reference itself|cycle"):
        resolve_run(wf, _cfg())


def test_differ_from_cycle_raises_loaderror() -> None:
    _OPENAI_PRESENT[0] = True
    wf = _wf(
        [_step("s1", "high", "a", differ_from="s2"),
         _step("s2", "high", "b", differ_from="s1")],
        {"a": _agent("a"), "b": _agent("b")},
    )
    with pytest.raises(LoadError, match="cycle"):
        resolve_run(wf, _cfg())


def test_no_provider_available_raises() -> None:
    """If every configured provider is env-unavailable, ProviderUnavailable
    surfaces at tier resolution."""
    _OPENAI_PRESENT[0] = False
    # only claude_code in config and we force its env_check off via re-register.
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "claude_code", env_check=lambda: False,
        factory=lambda m: _FakeProvider("claude_code", m))
    wf = _wf([_step("s1", "medium", "a")], {"a": _agent("a")})
    with pytest.raises(ProviderUnavailable):
        resolve_run(wf, _cfg(providers=("claude_code",)))


def test_differ_from_cascaded_chain() -> None:
    """A→B→C: C differs from B which itself has a per-step differ_from override.
    
    This exercises the branch in Pass 2 where the target step (B) has been placed
    in resolved_steps (because B had its own differ_from override), and then C 
    looks up B from resolved_steps rather than from resolved_tiers.
    """
    _OPENAI_PRESENT[0] = True
    wf = _wf(
        [
            _step("s1", "medium", "a"),
            _step("s2", "medium", "b", differ_from="s1"),
            _step("s3", "medium", "c", differ_from="s2"),
        ],
        {"a": _agent("a"), "b": _agent("b"), "c": _agent("c")},
    )
    tiers, steps, details = resolve_run(wf, _cfg())
    
    # s1 has no differ_from, so it comes from resolved_tiers["medium"]
    s1_resolved = tiers["medium"]
    assert s1_resolved.provider == "claude_code"
    
    # s2 differs from s1, so it's in resolved_steps and uses the other provider
    assert "s2" in steps
    assert steps["s2"].provider == "openai"
    assert details["steps"]["s2"]["differ_from"] == "s1"
    
    # s3 differs from s2's *resolved* provider. The key test: s3 looks up s2 from
    # resolved_steps (not resolved_tiers), so it correctly differs from openai.
    assert "s3" in steps
    assert steps["s3"].provider == "claude_code"
    assert details["steps"]["s3"]["differ_from"] == "s2"
    # s3 differs from s2's resolved provider (openai)
    assert steps["s3"].provider != steps["s2"].provider
