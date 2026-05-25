"""Agent runner protocol.

The engine knows only this protocol. Plan 1 shipped StubAgentRunner; Plan 2
adds DeepAgentRunner. The engine picks one based on env / config.

AgentInvocation carries everything the runner needs:
  - agent_def: parsed .md (output schema, declared tools, subagent refs, prompt template)
  - model_role + model: name (for logging) + resolved BaseChatModel
  - system_prompt: rendered Jinja2 template body (computed by the executor)
  - initial_messages: bootstrap messages
  - checkpointer: langgraph checkpointer (SqliteSaver per Change in production,
    MemorySaver in tests)
  - events_path: for the query_events tool
  - change_id / change_artifacts_dir / project_root: for tool wiring + paths
  - interactive: whether ChatMiddleware is installed

StubAgentRunner ignores the Plan 2 fields; DeepAgentRunner reads them all.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from imp.engine.types import AgentDef

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langgraph.checkpoint.base import BaseCheckpointSaver


@dataclass(frozen=True, slots=True)
class AgentInvocation:
    # Plan 1 fields
    agent_def: AgentDef
    model_role: str
    change_id: int
    change_artifacts_dir: Path
    project_root: Path
    interactive: bool

    # Plan 2 fields (defaults so StubAgentRunner tests don't need to set them)
    model: BaseChatModel | None = None
    system_prompt: str = ""
    initial_messages: tuple[dict[str, Any], ...] = ()
    events_path: Path | None = None
    checkpointer: BaseCheckpointSaver[Any] | None = None


class AgentRunner(Protocol):
    def run(self, invocation: AgentInvocation) -> Iterator[dict[str, Any]]:
        """Yield events as dicts with 'type' and 'payload'. The terminal event
        must be 'agent_output' (success) or 'error' (contract violation /
        runtime crash).
        """
        ...
