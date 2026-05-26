"""Anthropic provider (spec §6.9). Uses anthropic.AsyncAnthropic directly.

This file ships the Provider factory + the AnthropicAgentRunner stub.
Task 24 fills in the runner's run() body. The skeleton here is enough
for Task 23 (registration) and the provider-protocol tests.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from imp.agents.runner import AgentInvocation, Event


class AnthropicAgentRunner:
    def __init__(self, model_name: str, *, client_factory=None) -> None:
        self.model = model_name      # public — read by tests/engine
        self._client_factory = client_factory  # tests inject a fake
        self._cancel = False
        self._client = None  # built lazily on first run()

    async def run(self, invocation: AgentInvocation) -> AsyncIterator[Event]:
        # NOTE: This is a deliberately-unreachable skeleton. The `yield {}` after
        # `raise` makes Python treat run() as an async generator (otherwise the
        # `async for` callers would fail at type-time). Task 24 replaces this
        # whole body — do not try to drive this runner before Task 24 lands.
        raise NotImplementedError("AnthropicAgentRunner.run not implemented yet")
        yield {}  # type: ignore[unreachable]

    async def interrupt(self) -> None:
        self._cancel = True


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model_name: str, *, client_factory=None) -> None:
        self.model = model_name              # public — promoted from V0's `_model`
        self._client_factory = client_factory

    def create_runner(self, invocation: AgentInvocation) -> AnthropicAgentRunner:
        return AnthropicAgentRunner(
            model_name=self.model, client_factory=self._client_factory,
        )


def _env_check() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _factory(model: str) -> AnthropicProvider:
    return AnthropicProvider(model_name=model)
