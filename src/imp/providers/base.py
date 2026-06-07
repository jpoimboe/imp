"""Provider protocol. Resolved from config.providers: list and config.tiers: table."""

from __future__ import annotations

from typing import Protocol

from imp.agents.runner import AgentInvocation, AgentRunner


class Provider(Protocol):
    name: str   # "anthropic" | "openai" | "claude_code"
    model: str  # the resolved model id (e.g., "claude-sonnet-4-6")

    def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
        ...
