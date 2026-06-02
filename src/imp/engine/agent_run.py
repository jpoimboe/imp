"""Shared agent-invocation construction + drive loop.

One builder (`build_agent_invocation`) and one consumer (`drive_invocation`)
used by BOTH the executor (top-level steps) and dispatch_task (subagents), so
every agent — however spawned — gets the same completion contract, nudge,
tool-call ceiling, and error_policy BACKOFF resilience. (Invalid-output
recovery still differs: steps retry inline with feedback; subagents are
re-dispatched by the parent — see the dispatch_task caller.)

No-cycle invariant: at MODULE TOP this imports only cycle-free leaves —
`engine.types`, `engine.completion`, and `agents.runner` (which imports only
`engine.types`). `engine.error_policy` and `engine.schema` are imported LAZILY
inside `drive_invocation`, NOT at top: error_policy -> providers.errors eagerly
runs providers/__init__ -> anthropic -> agents.subagents, and agents.subagents
imports THIS module, so a top-level error_policy import would make a cold
`import imp.agents.subagents` crash with a partial-module ImportError. This
module must also NEVER import `engine.executor` or `agents.subagents` — both
import it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ErrorInfo:
    """The classified error that drove a fatal/backoff/retry decision, plumbed
    out of drive_invocation so the caller can record it on Pause and on the
    agent_retry event. None on the success path."""
    category: str
    kind: str | None


@dataclass(frozen=True)
class AgentRunResult:
    """The terminal outcome of driving one invocation. Carrying a dataclass
    (not a bool tuple) avoids arity bugs across the return statements."""
    output: dict[str, Any] | None
    errored: bool
    paused: bool
    fatal: bool
    delay: float | None
    error: ErrorInfo | None = None


class SubagentSuspended(Exception):
    """Raised by dispatch_task when a subagent's drive loop returns paused.

    Propagates (re-raised past each provider's task wrapper and outer run()
    catch) so the pause unwinds the stack: subagent -> dispatch_task -> parent
    run-loop (transcript left ending at the dangling `task` tool_use) ->
    parent drive_invocation -> executor, which persists status="paused".
    Resume re-enters the step fresh and re-dispatches the subagent subtree."""
