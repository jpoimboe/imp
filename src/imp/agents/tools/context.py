"""ToolContext (spec §6.6) — per-invocation state passed to every tool.

Virtual path conventions:
  /project/<rel>   → <project_root>/<rel>
  /artifacts/<rel> → <change_artifacts_dir>/<rel>
  /scratch/<rel>   → ToolContext.scratch dict (in-memory; lives for the step)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from imp.agents.permissions import check_permission
from imp.engine.types import AgentDef, FsRule


@dataclass
class ToolContext:
    project_root: Path
    artifacts_dir: Path
    fs_permissions: tuple[FsRule, ...]
    events_path: Path
    scratch: dict[str, str] = field(default_factory=dict)
    todos: list[dict[str, Any]] = field(default_factory=list)
    # Subagent dispatch fills these:
    subagent_defs: dict[str, AgentDef] = field(default_factory=dict)
    provider_name: str = ""
    model_name: str = ""
    parent_transcript_dir: Path | None = None
    interactive: bool = False
    # The change being processed; used by subagent dispatch to propagate change_id.
    change_context: Any = None
    # When the runner serves ask_user requests, it injects this callable.
    user_input_fn: Any = None
    # Subagent dispatch hook (filled by runner; signature defined in subagents tool).
    spawn_subagent: Any = None
    # invocation_id counter for disambiguating same-named subagent invocations
    _subagent_counter: int = 0

    def next_subagent_invocation_id(self) -> int:
        self._subagent_counter += 1
        return self._subagent_counter

    def resolve_path(self, virtual: str) -> Path | None:
        """Returns a real Path or None for /scratch/* (caller uses self.scratch)."""
        if virtual.startswith("/project/"):
            return self.project_root / virtual[len("/project/"):]
        if virtual.startswith("/artifacts/"):
            return self.artifacts_dir / virtual[len("/artifacts/"):]
        if virtual.startswith("/scratch/") or virtual == "/scratch":
            return None
        raise ValueError(
            f"virtual path must start with /project/, /artifacts/, or /scratch/; "
            f"got {virtual!r}"
        )

    def check_fs_permission(self, op: Literal["read", "write"], virtual: str) -> None:
        check_permission(self.fs_permissions, op, virtual)
