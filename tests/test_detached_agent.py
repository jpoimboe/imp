# tests/test_detached_agent.py
from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.agents.detached import (
    DetachedEnvironmentFailure,
    DetachedSuccess,
    DetachedTransientFailure,
    run_detached_agent,
)
from imp.engine.sandbox import SandboxConfig
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc

_SUCCESS_EVENTS = [
    {"type": "agent_result", "payload": {"status": "Success", "root": "src"}},
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _agent_def() -> AgentDef:
    return AgentDef(
        name="detect", description="", tools=frozenset(), subagents=(),
        output_schema=OutputSchema(
            fields=(
                OutputField(name="status", type="enum",
                            values=("Success", "Error"), required=True),
                OutputField(name="root", type="string", required=True),
            ),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="determine the import roots",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


class _ScriptedRunner:
    def __init__(self, events: list[dict[str, Any]], *, sleep_before: float | None = None) -> None:
        self._events = events
        self._sleep = sleep_before

    async def run(self, invocation: Any):
        if self._sleep is not None:
            await anyio.sleep(self._sleep)
        for ev in self._events:
            yield ev

    async def interrupt(self) -> None:
        pass


class _ScriptedProvider:
    name = "fake"

    def __init__(self, events: list[dict[str, Any]], *, model: str = "m",
                 sleep_before: float | None = None) -> None:
        self._events = events
        self._sleep = sleep_before
        self.model = model

    def create_runner(self, invocation: Any) -> _ScriptedRunner:
        return _ScriptedRunner(self._events, sleep_before=self._sleep)


def _register(provider_registry, events, *, sleep_before=None) -> None:
    provider_registry(
        "fake", env_check=lambda: True,
        factory=lambda model: _ScriptedProvider(
            events, model=model, sleep_before=sleep_before),
    )


async def _run(tmp_path: Path, *, cfg: SandboxConfig, timeout_s: float = 60.0):
    return await run_detached_agent(
        agent_def=_agent_def(),
        provider_name="fake", model_name="m",
        initial_message="go",
        project_root=tmp_path,
        workflow_dir=tmp_path,
        sandbox_cfg=cfg,
        timeout_s=timeout_s,
    )


@pytest.mark.anyio
async def test_success_returns_validated_output(tmp_path, provider_registry):
    _register(provider_registry, [
        {"type": "agent_result",
         "payload": {"status": "Success", "root": "src"}},
    ])
    result = await _run(tmp_path, cfg=SandboxConfig(allow_unsandboxed=True))
    assert isinstance(result, DetachedSuccess)
    assert result.output["status"] == "Success"
    assert result.output["root"] == "src"


@pytest.mark.anyio
async def test_initial_message_delivered_in_user_turn(tmp_path, provider_registry):
    """initial_message must appear in the user-turn text the agent actually sees."""
    captured: list[Any] = []

    class _CapturingProvider:
        name = "fake"
        model = "m"

        def create_runner(self, invocation: Any) -> _ScriptedRunner:
            captured.append(invocation)
            return _ScriptedRunner(_SUCCESS_EVENTS)

    provider_registry(
        "fake", env_check=lambda: True,
        factory=lambda model: _CapturingProvider(),
    )

    await run_detached_agent(
        agent_def=_agent_def(),
        provider_name="fake", model_name="m",
        initial_message="KICKOFF-DETECT-MARKER",
        project_root=tmp_path, workflow_dir=tmp_path,
        sandbox_cfg=SandboxConfig(allow_unsandboxed=True),
    )

    assert captured, "create_runner was never called"
    invocation = captured[0]
    assert invocation.initial_messages, "no user-turn messages in invocation"
    user_turn_text = invocation.initial_messages[0].content[0].text
    assert "KICKOFF-DETECT-MARKER" in user_turn_text


@pytest.mark.anyio
async def test_missing_sandbox_is_environment_failure(tmp_path, monkeypatch):
    import imp.agents.detached as detached
    monkeypatch.setattr(detached, "sandbox_available", lambda: False)
    # enabled sandbox required (allow_unsandboxed=False) but bwrap unavailable.
    result = await _run(tmp_path, cfg=SandboxConfig(allow_unsandboxed=False))
    assert isinstance(result, DetachedEnvironmentFailure)
    assert result.category == "sandbox"


@pytest.mark.anyio
async def test_auth_error_is_environment_failure(tmp_path, provider_registry):
    _register(provider_registry, [
        {"type": "error",
         "payload": {"category": "auth", "kind": "bad_key", "message": "no"}},
    ])
    result = await _run(tmp_path, cfg=SandboxConfig(allow_unsandboxed=True))
    assert isinstance(result, DetachedEnvironmentFailure)
    assert result.category == "auth"
    assert result.detail == "bad_key"


@pytest.mark.anyio
async def test_config_error_is_environment_failure(tmp_path, provider_registry):
    _register(provider_registry, [
        {"type": "error",
         "payload": {"category": "config", "kind": "rejected", "message": "no"}},
    ])
    result = await _run(tmp_path, cfg=SandboxConfig(allow_unsandboxed=True))
    assert isinstance(result, DetachedEnvironmentFailure)
    assert result.category == "config"
    assert result.detail == "rejected"
