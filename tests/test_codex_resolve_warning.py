from __future__ import annotations

from pathlib import Path

import pytest

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
from imp.providers.config import Config
from imp.providers.resolve import CODEX_SELECTION_WARNING, resolve_run

_SRC = SourceLoc(file=Path("w.yaml"), line=1)


class _FakeProvider:
    def __init__(self, name: str, model: str) -> None:
        self.name, self.model = name, model

    def create_runner(self, invocation):
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _providers():
    resolve.reset_providers_for_tests()
    resolve.register_provider(
        "claude_code", env_check=lambda: True,
        factory=lambda m: _FakeProvider("claude_code", m))
    resolve.register_provider(
        "codex", env_check=lambda: True,
        factory=lambda m: _FakeProvider("codex", m))
    yield
    resolve.reset_providers_for_tests()


def _agent(name: str) -> AgentDef:
    return AgentDef(
        name=name, description="", tools=frozenset(), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error")),), source=_SRC),
        prompt_template_source="", source_path=Path(f"{name}.md"),
        source=_SRC, tier=None)


def _step(name: str, tier: str, differ_from: str | None = None) -> Step:
    return Step(
        name=name, phase="p", kind="agent", rules=(), options=(),
        on_max_retries=Pause(), max_loops=6, source=_SRC,
        file=Path(f"agents/{name}.md"), tier=tier, differ_from=differ_from)


def _wf(steps, agents) -> Workflow:
    return Workflow(
        name="w", description="",
        phases=(Phase(name="p", steps=tuple(steps), source=_SRC),),
        vars=Vars(decls=()),
        defaults=WorkflowDefaults(max_retries=0, on_max_retries=Pause()),
        agents=agents, workflow_dir=Path("."), source=_SRC)


def _cfg(providers) -> Config:
    return Config(
        providers=tuple(providers),
        tiers={
            "high": {"claude_code": "opus", "codex": "gpt-5.5"},
        },
    )


def test_codex_tier_populates_warning() -> None:
    wf = _wf([_step("s1", "high")], {"s1": _agent("s1")})
    _tiers, _steps, details = resolve_run(wf, _cfg(("codex", "claude_code")))
    assert details["tiers"]["high"]["provider"] == "codex"
    assert details["tiers"]["high"]["warning"] == CODEX_SELECTION_WARNING
    assert "runs unsandboxed" in CODEX_SELECTION_WARNING


def test_non_codex_tier_omits_warning_key() -> None:
    wf = _wf([_step("s1", "high")], {"s1": _agent("s1")})
    _tiers, _steps, details = resolve_run(wf, _cfg(("claude_code", "codex")))
    assert details["tiers"]["high"]["provider"] == "claude_code"
    assert "warning" not in details["tiers"]["high"]


def test_codex_step_override_populates_warning() -> None:
    # s2 differs from s1; with only codex available it still resolves codex
    # and the step-details warning must be present.
    wf = _wf([_step("s1", "high"), _step("s2", "high", differ_from="s1")],
             {"s1": _agent("s1"), "s2": _agent("s2")})
    _tiers, _steps, details = resolve_run(wf, _cfg(("codex",)))
    assert details["steps"]["s2"]["provider"] == "codex"
    assert details["steps"]["s2"]["warning"] == CODEX_SELECTION_WARNING


def test_non_codex_step_override_omits_warning_key() -> None:
    # s1 resolves to codex (first available); s2 differs from s1 so it must pick
    # a different provider (claude_code). That non-codex step override must not
    # carry the warning key.
    wf = _wf([_step("s1", "high"), _step("s2", "high", differ_from="s1")],
             {"s1": _agent("s1"), "s2": _agent("s2")})
    _tiers, _steps, details = resolve_run(wf, _cfg(("codex", "claude_code")))
    assert details["steps"]["s2"]["provider"] == "claude_code"
    assert "warning" not in details["steps"]["s2"]
