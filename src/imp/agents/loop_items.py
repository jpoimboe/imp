# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Protocol

from imp.agents.loop_guard import LoopGuard
from imp.agents.runner import ToolResultBlock


@dataclass(frozen=True, slots=True)
class Thinking:
    text: str


@dataclass(frozen=True, slots=True)
class Warning:
    text: str


@dataclass(frozen=True, slots=True)
class ToolRequested:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AwaitingToolResults:
    assistant_text: str = ""


@dataclass(frozen=True, slots=True)
class EndOfTurn:
    last_text: str
    stop_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Error:
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DependencyWaited:
    deps: tuple[str, ...]


Item = (Thinking | Warning | ToolRequested | AwaitingToolResults | EndOfTurn
        | Error | DependencyWaited)


class Session(Protocol):
    """The push seam `run_agent_loop` drives. `events()` yields the items of ONE
    turn then returns; the driver acts and pushes results back via
    `send_tool_results` / `send`, then calls `events()` again for the next turn."""

    ctx: Any
    tools: dict[str, Any]
    elog: Any
    aid: str
    guard: LoopGuard
    ceiling: int | None
    tool_calls_used: int
    subagent_depth: int

    async def open(self) -> None: ...
    def cancelled(self) -> bool: ...
    def events(self) -> AsyncGenerator[Item, None]: ...
    def send_tool_results(self, results: list[ToolResultBlock]) -> None: ...
    def send(self, text: Any, *, human: bool) -> None: ...
    def deliver_turn_boundary_interject(self) -> None: ...
    def subagent_deliverable(self) -> str: ...
    def handoff_before_nudge(self, last_text: str) -> str | None: ...
    def handoff_after_tools(self) -> tuple[str, str | None]: ...
    def captured_complete_step(self) -> dict[str, Any] | None: ...
    def posthoc_ceiling_tripped(self) -> bool: ...
    def observe_bare_turn(self, last_text: str) -> Any: ...
