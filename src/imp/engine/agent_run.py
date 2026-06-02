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

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from imp.agents.runner import AgentInvocation, ChangeContext, Message
from imp.engine.completion import build_continuation_fn, completion_instruction
from imp.engine.types import AgentDef


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


def build_agent_invocation(
    *,
    agent_def: AgentDef,
    provider_name: str,
    model_name: str,
    rendered_system_prompt: str,
    initial_messages: tuple[Message, ...],
    change_context: ChangeContext,
    interactive: bool,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
    transcript_dir: Path,
    fs_permissions: Any,
    events_path: Path,
    subagent_defs: dict[str, AgentDef],
    model_tiers: tuple[str, ...],
    max_tool_calls: int | None,
    agent_id: str,
    event_log: Any,
    change_state: Any,
    first_event_parent_id: str | None,
    resolved_roles: dict[str, Any] | None,
    coordinator: Any,
    subagent_depth: int = 0,
    retry_feedback: str | None = None,
    balance_on_resume: bool = False,
    log: Any = None,
) -> AgentInvocation:
    """Construct one AgentInvocation, owning the completion-contract invariant
    that drifted between the two creation paths: append
    `completion_instruction(schema)` to the system prompt AND build the
    continuation_fn (interactive wrap vs. nudge) from that SAME instruction.

    The caller renders the prompt with its own PromptContext and resolves its
    own (provider, model) — the builder never branches on agent kind. `model`
    is a value, not a policy, here.
    """
    instruction = completion_instruction(agent_def.output_schema)
    system_prompt = f"{rendered_system_prompt}\n\n{instruction}"
    continuation_fn = build_continuation_fn(
        interactive=interactive,
        user_chat_input_fn=user_chat_input_fn,
        instruction=instruction,
        log=log,
        agent_id=agent_id,
    )
    return AgentInvocation(
        agent_def=agent_def,
        provider_name=provider_name,
        model_name=model_name,
        system_prompt=system_prompt,
        initial_messages=initial_messages,
        change_context=change_context,
        interactive=interactive,
        transcript_dir=transcript_dir,
        fs_permissions=fs_permissions,
        events_path=events_path,
        subagent_defs=subagent_defs,
        max_tool_calls=max_tool_calls,
        user_input_fn=user_chat_input_fn if interactive else None,
        continuation_fn=continuation_fn,
        agent_id=agent_id,
        event_log=event_log,
        change_state=change_state,
        first_event_parent_id=first_event_parent_id,
        retry_feedback=retry_feedback,
        balance_on_resume=balance_on_resume,
        resolved_roles=resolved_roles,
        model_tiers=model_tiers,
        subagent_depth=subagent_depth,
        coordinator=coordinator,
    )
