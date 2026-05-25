"""StubAgentRunner: Plan 1 implementation. Reads canned event streams from a
YAML fixture mapping agent name → list of events. Used only for tests until
Plan 2 ships the real deepagents adapter.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from imp.agents.runner import AgentInvocation


class StubAgentRunner:
    def __init__(self, canned_path: Path) -> None:
        self._canned: dict[str, dict[str, Any]] = yaml.safe_load(
            canned_path.read_text()
        )

    def run(self, invocation: AgentInvocation) -> Iterator[dict[str, Any]]:
        name = invocation.agent_def.name
        if name not in self._canned:
            raise KeyError(f"no canned output for agent {name!r}")
        yield from self._canned[name]["events"]
