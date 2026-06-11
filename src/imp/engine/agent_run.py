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
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio

from imp.agents.runner import AgentInvocation, ChangeContext, Message
from imp.agents.tools.limits import AGENT_INACTIVITY_TIMEOUT_S
from imp.agents.tools.wait_for_user import AwaitingUserInput
from imp.engine.completion import build_continuation_fn, completion_instruction
from imp.engine.types import AgentDef

# Injected into the system prompt of every agent that has the `bash` tool (see
# build_agent_invocation). This is a PROPERTY OF HOLDING `bash`, not something
# each prompt author must remember to paste — code-reviewer and summarizer once
# lacked path guidance and burned tool calls feeding bad paths to `ls`/`find`/
# `head`/`sed`. The path model is worktree-relative (Part A): no `/project`.
_BASH_VIRTUAL_PATHS_NOTE = """\
## Paths are relative to the project root

Your file tools (`read_file`, `write_file`, `edit_file`, `grep`, `glob`, `ls`)
take paths **relative to the project root** (e.g. `src/foo.py`, `imp/specs/x.md`).
Absolute paths and `..` escapes outside the project root are rejected.

The `bash` tool already runs with its working directory set to the project root,
so use **relative paths** there too (`cat src/foo.py`, `cd src && ...`). When you
need an absolute path on disk, use `$IMP_PROJECT_ROOT` (the real root path)."""


# Injected into the system prompt of EVERY agent (top-level or subagent),
# unconditional on tool set. Documents the `<imp:begin/>` opener as an
# engine-emitted signal — the executor seeds the first user-role message
# with this literal tag (see executor._route_agent) rather than a fake
# user "begin" prompt. Without this note the model sees an unfamiliar token
# and has to infer intent; with it, the contract is documented where the
# model reads.
_ENGINE_SEED_NOTE = """\
## Session start: `<imp:begin/>`

Your first user-role message in this conversation is the literal token
`<imp:begin/>`. It is emitted by the engine to open the turn — it is not
a message a human user typed. Treat it as a signal to begin the work
described in the rest of this system prompt; do not reply to it as if a
user had addressed you, and do not quote it back."""


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
    # Headless wait_for_user pause: paused=True drives normal pause handling;
    # awaiting_user=True additionally tells the executor to mark THIS step
    # resumable (Task 3 Step 5) so the dangling wait_for_user tool_use survives
    # the reload. Stays False for every other pause (coordinator, subagent).
    awaiting_user: bool = False


class SubagentSuspended(Exception):
    """Raised by dispatch_task when a subagent's drive loop returns paused.

    Propagates (re-raised past each provider's task wrapper and outer run()
    catch) so the pause unwinds the stack: subagent -> dispatch_task -> parent
    run-loop (transcript left ending at the dangling `task` tool_use) ->
    parent drive_invocation -> executor, which persists status="paused".
    Resume re-enters the step fresh and re-dispatches the subagent subtree."""


async def _pull_with_inactivity_watchdog(
    gen: Any, *, runner: Any, stall: dict[str, bool], coordinator: Any = None,
) -> AsyncIterator[Any]:
    """Yield events from `gen`, tearing the agent down only after
    AGENT_INACTIVITY_TIMEOUT_S of genuine inactivity (B.2).

    Each pull is bounded by `anyio.fail_after(window)`, but the deadline is NOT
    fixed at pull start: while we await the next yield, any append to the shared
    event log by this agent OR a descendant fires `coordinator.note_activity()`,
    which pushes this scope's deadline forward. So a parent parked in an
    in-process subagent dispatch — and a busy claude_code leaf that appends to
    elog without yielding (neither produces generator yields) — is kept alive by
    its own activity instead of being falsely killed at start+window. A genuinely
    hung agent (no self/subtree appends for the window) still times out: the pull
    is cancelled, we drive the agent's OWN teardown (runner interrupt + generator
    aclose) so the in-flight work actually dies, set stall['hit'], and end
    iteration. `fail_after` raises TimeoutError only on ITS deadline, so an
    OUTER cancellation (pause/interrupt) propagates untouched, not as a stall.

    coordinator may be None, or a partial/legacy coordinator without the activity
    API: the deadline then resets only on each generator yield — exactly the
    pre-liveness behavior."""
    add_listener = getattr(coordinator, "add_activity_listener", None)
    remove_listener = getattr(coordinator, "remove_activity_listener", None)
    while True:
        try:
            with anyio.fail_after(AGENT_INACTIVITY_TIMEOUT_S) as scope:
                bump = None
                if add_listener is not None:
                    def _bump(_scope: Any = scope) -> None:
                        # Read the module global at call time so a monkeypatched
                        # window (tests) and the live value agree. While an
                        # attended agent is blocked awaiting a human reply, FREEZE
                        # the in-flight deadline to inf (disabling this fail_after
                        # entirely) instead of a finite now+window: there is no
                        # periodic activity during the blocked await, so a one-shot
                        # finite bump would expire one window later and re-introduce
                        # the false kill. end_solicit_watchdog's note_activity then
                        # re-fires this with solicit_active False, restoring a fresh
                        # finite window.
                        if getattr(coordinator, "solicit_active", False):
                            _scope.deadline = math.inf
                        else:
                            _scope.deadline = (
                                anyio.current_time() + AGENT_INACTIVITY_TIMEOUT_S)
                    bump = add_listener(_bump)
                try:
                    ev = await gen.__anext__()
                finally:
                    if bump is not None and remove_listener is not None:
                        remove_listener(bump)
        except StopAsyncIteration:
            return
        except TimeoutError:
            stall["hit"] = True
            with contextlib.suppress(Exception):
                await runner.interrupt()
            with contextlib.suppress(Exception):
                await gen.aclose()
            return
        yield ev


def deliver_interject(coordinator: Any, messages: Any, transcript: Any,
                      elog: Any, agent_id: str) -> bool:
    """If a human interject is queued, append it as a user message and emit
    user_interjected. Returns True if one was delivered. Called at the agent's
    turn boundary (not mid-turn, not while awaiting a subagent)."""
    if coordinator is None:
        return False
    text = coordinator.drain_interject()
    if text is None:
        return False
    from imp.agents.runner import Message, TextBlock
    msg = Message(role="user", content=(TextBlock(text=text),))
    messages.append(msg)
    transcript.append(msg)
    elog.append_user_message(agent_id=agent_id, message=msg)
    elog.append(agent_id=agent_id, type="user_interjected", payload={"text": text})
    return True


def _join_pending_interjects(coordinator: Any) -> str | None:
    """Drain ALL queued interjects and join them newline-separated into ONE
    string, or None if the queue is empty. Used to satisfy a dangling
    wait_for_user on resume (§2 dangling-wait routing)."""
    if coordinator is None:
        return None
    parts: list[str] = []
    while True:
        t = coordinator.drain_interject()
        if t is None:
            break
        parts.append(t)
    return "\n".join(parts) if parts else None


async def _resolve_wait_reply(
    coordinator: Any,
    user_input_fn: Callable[[str], str | Awaitable[str]],
) -> str:
    """Satisfy a dangling wait_for_user on resume (§2 F5, R6 pending-satisfies-
    solicit): if the interject queue is non-empty, drain+join it as the reply;
    otherwise solicit via user_input_fn. Returns the reply string the caller
    then journals + replays."""
    import inspect
    joined = _join_pending_interjects(coordinator)
    if joined is not None:
        return joined
    raw = user_input_fn("")
    if inspect.isawaitable(raw):
        raw = await raw
    return str(raw)


def drain_all_interjects_at_resume(
    coordinator: Any, messages: Any, transcript: Any, elog: Any, agent_id: str,
) -> int:
    """Resume-entry drain (§2): deliver EVERY queued interject in order as user
    messages, folded into the first request, and CLEAR the queue so the
    following turn-boundary drain is a no-op (no double-delivery). Returns the
    count delivered. Mid-turn shape (messages-list providers: anthropic/openai).
    The dangling-wait join is handled separately by the resume classifier (F5)."""
    if coordinator is None:
        return 0
    from imp.agents.runner import Message, TextBlock
    n = 0
    while True:
        text = coordinator.drain_interject()
        if text is None:
            break
        msg = Message(role="user", content=(TextBlock(text=text),))
        messages.append(msg)
        transcript.append(msg)
        elog.append_user_message(agent_id=agent_id, message=msg)
        elog.append(agent_id=agent_id, type="user_interjected", payload={"text": text})
        n += 1
    return n


def build_agent_invocation(
    *,
    agent_def: AgentDef,
    provider_name: str,
    model_name: str,
    rendered_system_prompt: str,
    system_prompt_override: str | None = None,
    initial_messages: tuple[Message, ...],
    change_context: ChangeContext,
    user_chat_input_fn: Callable[[str], str | Awaitable[str]] | None,
    transcript_dir: Path,
    fs_permissions: Any,
    events_path: Path,
    subagent_defs: dict[str, AgentDef],
    tiers: tuple[str, ...],
    max_tool_calls: int | None,
    agent_id: str,
    event_log: Any,
    change_state: Any,
    change: Any = None,
    first_event_parent_id: str | None,
    resolved_tiers: dict[str, Any] | None,
    coordinator: Any,
    subagent_depth: int = 0,
    retry_feedback: str | None = None,
    balance_on_resume: bool = False,
    log: Any = None,
    lint_command: str | None = None,
    test_command: str | None = None,
    non_interactive: bool = False,
    sandbox_policy: Any = None,
) -> AgentInvocation:
    """Construct one AgentInvocation, owning the completion-contract invariant
    that drifted between the two creation paths: append
    `completion_instruction(schema)` to the system prompt AND build the
    continuation_fn (always the nudge backstop) from that SAME instruction.

    The caller renders the prompt with its own PromptContext and resolves its
    own (provider, model) — the builder never branches on agent kind. `model`
    is a value, not a policy, here.
    """
    instruction = completion_instruction(agent_def.output_schema)
    if system_prompt_override is not None:
        # Resume-replay (Part B): the override IS the previously-assembled,
        # stored step_context.system_prompt — use it verbatim, never re-append
        # the bash/seed/instruction notes (that would double them).
        system_prompt = system_prompt_override
    else:
        body = rendered_system_prompt
        if "bash" in agent_def.tools:
            body = f"{body}\n\n{_BASH_VIRTUAL_PATHS_NOTE}"
        body = f"{body}\n\n{_ENGINE_SEED_NOTE}"
        system_prompt = f"{body}\n\n{instruction}"
    continuation_fn = build_continuation_fn(instruction=instruction)
    return AgentInvocation(
        agent_def=agent_def,
        provider_name=provider_name,
        model_name=model_name,
        system_prompt=system_prompt,
        initial_messages=initial_messages,
        change_context=change_context,
        transcript_dir=transcript_dir,
        fs_permissions=fs_permissions,
        events_path=events_path,
        subagent_defs=subagent_defs,
        max_tool_calls=max_tool_calls,
        user_input_fn=user_chat_input_fn,
        continuation_fn=continuation_fn,
        agent_id=agent_id,
        event_log=event_log,
        change_state=change_state,
        change=change,
        first_event_parent_id=first_event_parent_id,
        retry_feedback=retry_feedback,
        balance_on_resume=balance_on_resume,
        resolved_tiers=resolved_tiers,
        tiers=tiers,
        subagent_depth=subagent_depth,
        coordinator=coordinator,
        lint_command=lint_command,
        test_command=test_command,
        non_interactive=non_interactive,
        sandbox_policy=sandbox_policy,
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
    stall = {"hit": False}
    attach = (coordinator.runner_attached(runner.interrupt)
              if coordinator is not None else contextlib.nullcontext())
    gen = runner.run(invocation)
    try:
        try:
            with attach:
                async for event in _pull_with_inactivity_watchdog(
                        gen, runner=runner, stall=stall,
                        coordinator=coordinator):
                    if event["type"] == "tool_heartbeat":
                        # Watchdog-only event: consumed-and-dropped BEFORE both
                        # consumers. Not appended (one per tool call would bloat
                        # the log and feed an unknown type to NarrativeBuilder
                        # and replay readers); NOT a pause point (pausing here
                        # would return mid-assistant-turn, abandoning the
                        # generator between tools with a dangling tool_use in
                        # the transcript).
                        continue
                    if emit_events and log is not None:
                        log.append(agent_id=invocation.agent_id,
                                   type=event["type"],
                                   payload=event.get("payload", {}))
                    if event["type"] == "agent_output":
                        agent_output = event["payload"]
                    if event["type"] == "error":
                        payload = event.get("payload") or {}
                        category = payload.get("category", "unknown")
                        kind = payload.get("kind")
                        retry_after = (payload.get("detail") or {}).get(
                            "retry_after_seconds")
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
        finally:
            # Close the generator on EVERY exit — the error/retry return, the
            # pause return, SubagentSuspended/AwaitingUserInput — so run()'s
            # finally (SDK client close + background-process teardown) runs
            # NOW, before any re-drive of the same invocation, not at
            # nondeterministic GC finalization. The retried invocation's
            # namespace-reuse safety (bg counter seeding) depends on attempt
            # 1's teardown completing first.
            with contextlib.suppress(Exception):
                await gen.aclose()
    except SubagentSuspended:
        # A nested subagent paused; unwind this drive as paused too.
        return AgentRunResult(None, False, True, False, None)
    except AwaitingUserInput:
        # A headless/subagent wait_for_user yielded with no human attached.
        # paused=True drives normal pause handling; awaiting_user=True tells the
        # executor to mark the step resumable (Step 5) so the dangling
        # wait_for_user tool_use survives reload.
        return AgentRunResult(None, False, True, False, None, awaiting_user=True)

    if stall["hit"]:
        # B.2: the watchdog tore the agent down. Surface as errored/'stalled'.
        if emit_events and log is not None:
            log.append(agent_id=invocation.agent_id, type="error",
                       payload={"category": "config", "kind": "stalled",
                                "message": f"no events for {AGENT_INACTIVITY_TIMEOUT_S:.0f}s"})
        return AgentRunResult(None, True, False, False, None,
                              ErrorInfo("config", "stalled"))

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
