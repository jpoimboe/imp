"""Scripted provider: a deterministic, offline AgentRunner for tests and CI."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

from imp.agents.runner import (
    AgentInvocation,
    AgentRunner,
    Event,
)
from imp.providers.base import Provider

ENV_VAR = "IMP_SCRIPTED_SCRIPT"
TIER_DEFAULTS: dict[str, str] | None = None


def _env_check() -> bool:
    return bool(os.environ.get(ENV_VAR))



class ScriptedAgentRunner:
    def __init__(self, model_name: str) -> None:
        self.model = model_name

    async def run(
        self, invocation: AgentInvocation,
    ) -> AsyncGenerator[Event, None]:
        raise NotImplementedError("ScriptedAgentRunner.run lands in Task 2")
        yield  # type: ignore[unreachable]  # pragma: no cover

    async def interrupt(self) -> None:
        pass


class ScriptedProvider:
    name = "scripted"

    def __init__(self, model_name: str) -> None:
        self.model = model_name

    def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
        return ScriptedAgentRunner(model_name=self.model)


def _factory(model: str) -> Provider:
    return ScriptedProvider(model_name=model)
