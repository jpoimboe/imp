from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, AgentRunner
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
from imp.providers.resolve import resolve_run

_SRC = SourceLoc(file=Path("w.yaml"), line=1)


class _FakeProvider:
    def __init__(self, name: str, model: str) -> None:
        self.name, self.model = name, model

    def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _providers() -> Iterator[None]:
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
        name=name, phase="p", kind="agent", rules=(),
        on_max_retries=Pause(), max_loops=6, source=_SRC,
        file=Path(f"agents/{name}.md"), tier=tier, differ_from=differ_from)


def _wf(steps: list[Step], agents: dict[str, AgentDef]) -> Workflow:
    return Workflow(
        name="w", description="",
        phases=(Phase(name="p", steps=tuple(steps), source=_SRC),),
        vars=Vars(decls=()),
        defaults=WorkflowDefaults(max_retries=0, on_max_retries=Pause()),
        agents=agents, workflow_dir=Path("."), source=_SRC)


def _cfg(providers: tuple[str, ...]) -> Config:
    return Config(
        providers=tuple(providers),
        tiers={"high": {"claude_code": "opus", "codex": "gpt-5.5"}},
    )


def test_codex_tier_detail_carries_no_warning_key() -> None:
    wf = _wf([_step("s1", "high")], {"s1": _agent("s1")})
    _tiers, _steps, details = resolve_run(wf, _cfg(("codex", "claude_code")))
    assert details["tiers"]["high"] == {
        "provider": "codex", "model": "gpt-5.5",
        "differ_from": None, "differ_tier": None}


def test_codex_step_override_detail_carries_no_warning_key() -> None:
    # s2 differs from s1 with only codex configured, so the override still
    # resolves codex (differ tier 3 = identical) — the case that used to get
    # its own warning line.
    wf = _wf([_step("s1", "high"), _step("s2", "high", differ_from="s1")],
             {"s1": _agent("s1"), "s2": _agent("s2")})
    _tiers, _steps, details = resolve_run(wf, _cfg(("codex",)))
    assert details["steps"]["s2"] == {
        "provider": "codex", "model": "gpt-5.5",
        "differ_from": "s1", "differ_tier": 3}
