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

import contextlib
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
    lint_command: str | None = None,
    test_command: str | None = None,
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
        lint_command=lint_command,
        test_command=test_command,
    )


async def drive_invocation(
    invocation: AgentInvocation,
    runner: Any,
    *,
    coordinator: Any,
    log: Any,
    emit_events: bool,
    prior_backoff_attempts: int = 0,
) -> AgentRunResult:
    """Drive one invocation to a single terminal outcome.

    Consumes runner.run(), routes each error event through error_policy.decide
    (Pause -> fatal; RetryWithBackoff -> delay; RetryNow -> errored), captures
    agent_output, and detects a coordinator-requested pause. A SubagentSuspended
    raised by a nested subagent propagates up through runner.run() and is
    caught here as a pause (so the executor persists status="paused").

    The caller owns the backoff loop: on a non-None `delay`, sleep and re-drive
    with `prior_backoff_attempts + 1` (a FRESH runner — runners are single-use).

    emit_events: when True (the executor), each drained event is appended to
    `log` with agent_id=invocation.agent_id and synthesized validation errors
    with agent_id=None — exactly today's behavior. False (subagents) keeps the
    subagent's terminal events out of the parent's stream, preserving today's
    dispatch behavior; the runner still writes its own audit events to its log.

    coordinator may be None (legacy/test): interrupt attachment is skipped and
    no pause is detected.
    """
    # LAZY imports (see module docstring): importing error_policy at module top
    # would pull in providers/__init__ -> anthropic -> agents.subagents and crash
    # a cold `import imp.agents.subagents`. drive_invocation is never called at
    # import time.
    from imp.engine import error_policy
    from imp.engine.schema import validate_agent_output

    agent_output: dict[str, Any] | None = None
    attach = (coordinator.runner_attached(runner.interrupt)
              if coordinator is not None else contextlib.nullcontext())
    try:
        with attach:
            async for event in runner.run(invocation):
                if emit_events and log is not None:
                    log.append(agent_id=invocation.agent_id,
                               type=event["type"], payload=event.get("payload", {}))
                if event["type"] == "agent_output":
                    agent_output = event["payload"]
                if event["type"] == "error":
                    payload = event.get("payload") or {}
                    category = payload.get("category", "unknown")
                    kind = payload.get("kind")
                    retry_after = (payload.get("detail") or {}).get("retry_after_seconds")
                    decision = error_policy.decide(
                        category, prior_backoff_attempts=prior_backoff_attempts,
                        hint_delay_seconds=retry_after)
                    if isinstance(decision, error_policy.Pause):
                        return AgentRunResult(None, True, False, True, None,
                                              ErrorInfo(category, kind))
                    if isinstance(decision, error_policy.RetryWithBackoff):
                        return AgentRunResult(None, True, False, False,
                                              decision.delay_seconds,
                                              ErrorInfo(category, kind))
                    # RetryNow (agent_output_missing today)
                    return AgentRunResult(None, True, False, False, None,
                                          ErrorInfo(category, kind))
                if coordinator is not None and coordinator.should_pause:
                    return AgentRunResult(None, False, True, False, None)
    except SubagentSuspended:
        # A nested subagent paused; unwind this drive as paused too.
        return AgentRunResult(None, False, True, False, None)

    if agent_output is None:
        return AgentRunResult(None, True, False, False, None)

    try:
        validate_agent_output(agent_output, invocation.agent_def.output_schema)
    except Exception as e:
        if emit_events and log is not None:
            log.append(agent_id=None, type="error", payload={
                "category": "agent_output_missing",
                "kind": "output_validation",
                "message": f"output validation: {e}"})
        return AgentRunResult(None, True, False, False, None,
                              ErrorInfo("agent_output_missing", "output_validation"))

    return AgentRunResult(agent_output, False, False, False, None)
