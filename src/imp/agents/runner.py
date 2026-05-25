"""Agent runner protocol.

The engine knows only this protocol. Plan 2 replaces StubAgentRunner with a
deepagents adapter without changing any engine code.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from imp.engine.types import AgentDef


@dataclass(frozen=True, slots=True)
class AgentInvocation:
    agent_def: AgentDef
    model_role: str
    change_id: int
    change_artifacts_dir: Path
    project_root: Path
    interactive: bool


class AgentRunner(Protocol):
    def run(self, invocation: AgentInvocation) -> Iterator[dict[str, Any]]:
        """Yield events as dicts with 'type' and 'payload'. The terminal event
        must be `agent_output` (success) or `error` (contract violation /
        runtime crash).
        """
        ...
