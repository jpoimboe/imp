"""Claude Code provider + runner (spec §6.11).

Drives `claude -p` via claude_agent_sdk. Differences from the direct-SDK
runners:

  - Tools are exposed via an in-process MCP server (`create_sdk_mcp_server`).
    Each imp Tool gets a thin async wrapper that runs the underlying sync
    function in the same process; the MCP transport is stdio between
    `claude -p` and our Python process.
  - History replay via stream-json input — `ClaudeSDKClient.query(...)`
    accepts an AsyncIterable[dict] that the SDK writes to claude's stdin
    one JSON line per dict. Verified end-to-end by Spike 7 (replayed
    tool_use blocks are NOT re-dispatched).
  - `complete_step` is a synthesized MCP tool like any other. Its wrapper
    raises StepComplete; the SDK surfaces tool exceptions as `is_error: true`
    tool_results, so the runner watches for the next assistant message and
    extracts the captured args from a shared state dict.

History writes are suppressed differently depending on run mode.  Fresh starts
run against an isolated ephemeral `CLAUDE_CONFIG_DIR` created by
`materialize_fresh_config_dir` and removed in `run()`'s finally block; with a
private home directory the suppression flags are unnecessary.  Resume and
stream-json-replay paths have no ephemeral config dir, so they keep
`CLAUDE_CODE_SKIP_PROMPT_HISTORY=1` in the subprocess env and pass
`extra_args={"no-session-persistence": None}` (the documented forwarding path
for `--no-session-persistence`; spec §6.11, verified in Spike 7 and SDK
`_internal/transport/subprocess_cli.py`) to avoid writing to `~/.claude`
(CC 2.x otherwise writes an `ai-title` session stub to ~/.claude/projects/).
Imp persists its own transcripts.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import re
import shutil
import signal
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from imp.agents.classify import ResumeState, classify_resume
from imp.agents.loop_guard import (
    LoopGuard,
    ToolOutcome,
    TurnRecord,
    _result_preview,
)
from imp.agents.parallel import max_parallel_tasks
from imp.agents.runner import (
    AgentInvocation,
    AgentRunner,
    Diagnostics,
    Event,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from imp.agents.subagents import (
    SubagentSuspended,
    dispatch_task,
    make_task_tool,
    raise_if_suspended,
)
from imp.agents.tools import Tool, get_registry
from imp.agents.tools import bg as _bg
from imp.agents.tools.complete_step import make_complete_step_tool
from imp.agents.tools.context import ToolContext, change_home_path, kill_process_group
from imp.agents.tools.errors import FatalToolError
from imp.agents.tools.load_skill import make_load_skill_tool
from imp.agents.tools.wait_for_user import AwaitingUserInput
from imp.agents.transcript import (
    RESUME_MANIFEST_HEADER,
    Transcript,
    balance_success_on_resume,
    build_resume_manifest,
    deliver_reply_on_resume,
    repair_dangling_tool_use_on_resume,
)
from imp.engine.agent_run import (
    _resolve_wait_reply,
    build_inline_solicit,
    drain_all_interjects_at_resume,
)
from imp.engine.drivers import driver_allowed_tools
from imp.engine.schema import validate_agent_output
from imp.providers import errors as perr
from imp.providers._cc_builtins import ALL_CLAUDE_CODE_BUILTIN_TOOLS
from imp.providers.base import Provider
from imp.providers.claude_code_config_dir import materialize_fresh_config_dir
from imp.providers.claude_code_session_store import (
    resume_session_id,
    session_key,
    session_store_for,
    store_has_conversation_body,
)
from imp.providers.resolve import spawn_subagent_via_registry
from imp.providers.web_native import (
    CC_WEB_BUILTIN_TO_NORM,
    WEB_TOOL_NAMES,
    cc_enabled_web_builtins,
    check_web_capability,
)

_AUTH_PATTERNS = (
    "Invalid API key",
    "Please run `/login`",
    "Invalid authentication credentials",
    "Failed to authenticate",
)

_QUOTA_PATTERNS = (
    "You have reached your usage limit",
    "Credit balance is too low",
)

# Matches "API Error: <code>" surfaced by the claude CLI as assistant text when
# the underlying Anthropic API returns a non-2xx. 401/403 → AuthError; 408 and
# 5xx → NetworkError; 429 → RateLimitError; other 4xx → ApiError (Pause);
# previously the 5xx case fell through to None and was treated as agent text →
# wrong nudge loop.
_API_ERROR_RE = re.compile(r"API Error:\s*(\d{3})(?!\d)")


def _classify_agent_text(text: str) -> perr.ProviderError | None:
    """Detect known claude CLI terminal-error patterns in agent text.

    The CLI catches its own errors and returns them as a text response
    with exit 0 (no out-of-band signal). Exact-substring patterns,
    conservatively chosen: false negatives fall back to existing
    retry behavior; false positives would convert recoverable agent
    confusion into fatal pause.

    Auth/quota patterns are checked first so they win when both substrings
    appear. The API-error regex matches "API Error: <code>":
    401/403 → AuthError with kind="http_<code>"; 408 → NetworkError (transient
    timeout); 429 → RateLimitError; other 4xx → ApiError with kind="http_<code>";
    5xx → NetworkError (network category → RetryWithBackoff; §10).
    """
    for pat in _AUTH_PATTERNS:
        if pat in text:
            return perr.AuthError(f"claude CLI: {pat}")
    for pat in _QUOTA_PATTERNS:
        if pat in text:
            return perr.QuotaError(f"claude CLI: {pat}")
    m = _API_ERROR_RE.search(text)
    if m:
        code = int(m.group(1))
        if code == 429:
            return perr.RateLimitError(f"claude CLI: {text[:200]}")
        if code in (401, 403):
            auth_exc = perr.AuthError(f"claude CLI: {text[:200]}")
            auth_exc.kind = f"http_{code}"  # type: ignore[attr-defined]
            return auth_exc
        if code == 408:
            return perr.NetworkError(f"claude CLI: {text[:200]}")
        if 400 <= code < 500:
            exc = perr.ApiError(f"claude CLI: {text[:200]}")
            # Carried through to make_error_event(kind=...) at the call sites so
            # the event records kind="http_400" rather than the class name.
            exc.kind = f"http_{code}"  # type: ignore[attr-defined]
            return exc
        if 500 <= code < 600:
            # §10: 5xx (529 Overloaded et al.) is transient — route to the
            # `network` category, which maps to RetryWithBackoff (same routing the
            # SDK providers use for an InternalServerError).
            return perr.NetworkError(f"claude CLI: {text[:200]}")
    elif "API Error:" in text:
        # A statusless `API Error:` — no routable HTTP code — means the request
        # never reached the server (ConnectionRefused, reset, EOF, transport
        # timeout). Broad transport stance: transient -> retry, not pause. A
        # 3xx/unrouted code that matched the regex above is NOT statusless, so
        # it falls through to None (Pause) here.
        return perr.NetworkError(f"claude CLI: {text[:200]}")
    return None


def _termination_from_result(
    sdk_msg: Any, last_text: str, diag: dict[str, Any],
) -> perr.ProviderError | None:
    """P2: a ResultMessage flagged is_error (or a nonzero subprocess exit) is a
    real termination — return a typed-or-unknown ProviderError carrying every
    available structured field in the detail. Returns None when there is no
    termination signal, so the caller falls through to the benign nudge path.

    Detection keys on is_error / exit code, never on the wording, so a usage
    limit reports regardless of its exact phrasing. The substring classifier is
    only the last-resort labeler: a recognized quota/auth keeps its type;
    anything else is the generic TerminatedError."""
    is_error = bool(getattr(sdk_msg, "is_error", False))
    exit_code = diag.get("exit")
    nonzero_exit = isinstance(exit_code, int) and exit_code != 0
    if not (is_error or nonzero_exit):
        return None
    detail: dict[str, object] = {
        "is_error": is_error,
        "subtype": getattr(sdk_msg, "subtype", None),
        "api_error_status": getattr(sdk_msg, "api_error_status", None),
        "stop_reason": getattr(sdk_msg, "stop_reason", None),
        "last_agent_text": last_text[:500],
        "exit_code": exit_code,
        "stderr_tail": diag.get("stderr_tail"),
        "errors": getattr(sdk_msg, "errors", None),
    }
    labeled = _classify_agent_text(last_text)
    status = detail["api_error_status"]
    if labeled is None and isinstance(status, int) and 500 <= status < 600:
        labeled = perr.NetworkError(f"claude_code: api_error_status={status}")
    if labeled is None and status in (401, 403):
        labeled = perr.AuthError(f"claude_code: api_error_status={status}")
        labeled.kind = f"http_{status}"  # type: ignore[attr-defined]
    if labeled is None and status == 429:
        labeled = perr.RateLimitError(f"claude_code: api_error_status={status}")
    if isinstance(labeled, (perr.QuotaError, perr.AuthError,
                            perr.NetworkError, perr.RateLimitError)):
        labeled.detail.update(detail)
        return labeled
    return perr.TerminatedError(
        "claude_code agent terminated with an error (cause unknown)",
        detail=detail,
    )


def _roll_back_error_turn(
    messages: list[Message], transcript: Transcript,
) -> None:
    """Drop the trailing assistant error turn so a backoff re-drive re-queries."""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "assistant":
            del messages[i]
            break
    transcript.rewrite(messages)


def _text_of(message: Any) -> str:
    """Concatenate TextBlock contents from a Message; ignore other block types.

    Used as input to _classify_agent_text. Joins blocks with newlines so
    consecutive text blocks remain visually separated for pattern matching.
    """
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
    return "\n".join(parts)


def _drain_interject_message(coordinator: Any, elog: Any,
                             agent_id: str) -> Message | None:
    """claude_code variant of agent_run.deliver_interject. The SDK is queried
    with a STRING, not a messages list, so the caller folds the returned
    message's text into the next query itself — here we only drain the queue,
    returning a user Message (or None when empty). The caller appends it to
    messages/transcript and emits the `user_message` THEN `user_interjected`
    events (in that order, uniform with deliver_interject and codex) so a log
    consumer sees `user_message` before `user_interjected` across all providers,
    and the persisted history matches the nudge re-query path exactly."""
    if coordinator is None:
        return None
    text = coordinator.drain_interject()
    if text is None:
        return None
    return Message(role="user", content=(TextBlock(text=text),))


def _subprocess_diag(client: Any) -> dict[str, Any]:
    """Best-effort extraction of subprocess exit/stderr from an SDK client for
    the no_result_message error detail. The SDK's transport surface is not a
    stable contract, so probe defensively and return {} when nothing is found.

    Keys are 'exit' and 'stderr_tail' — deliberately disjoint from
    'retry_after_seconds', which the executor reads from detail (executor.py).

    SDK inspection (claude_agent_sdk, installed 2026-05-30):
      ClaudeSDKClient._transport → SubprocessCLITransport (when connected)
      SubprocessCLITransport does NOT expose 'returncode' or 'stderr_tail' as
      public attributes. The process returncode lives at _process.returncode
      (private, anyio.Process). No public stderr_tail attribute exists.
      The _transport is set to None by disconnect() in __aexit__, so by the
      time the fallthrough emit runs the transport is already gone. The helper
      therefore returns {} against today's SDK — the enrichment is intentionally
      forward-compatible plumbing. The fake-shape (returncode/stderr_tail on
      _transport) tests the plumbing path for future SDK versions that may
      expose these attributes.
    """
    out: dict[str, Any] = {}
    transport = getattr(client, "_transport", None) or client
    rc = getattr(transport, "returncode", None)
    if isinstance(rc, int):
        out["exit"] = rc
    tail = getattr(transport, "stderr_tail", None)
    if tail:
        out["stderr_tail"] = str(tail)[-2000:]
    return out


_REAP_GRACE_SECONDS = 0.5


async def _reap_transport_group(client: Any) -> None:
    """SIGTERM→SIGKILL the transport child's process group; no-op when absent."""
    transport = getattr(client, "_transport", None)
    process = getattr(transport, "_process", None)
    if process is None:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        await kill_process_group(process, signal.SIGTERM)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=_REAP_GRACE_SECONDS)
            return  # exited within grace; already reaped
        await kill_process_group(process, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await process.wait()


def _is_terminated_process_teardown(exc: BaseException) -> bool:
    """A `CLIConnectionError: Cannot write to terminated process` from the claude
    SDK means a (process-group) interrupt already killed the CLI child cleanly and
    the SDK *then* tried to write to it — interrupt teardown, never a recoverable
    provider failure. Recognizing it lets the runner classify it as a Pause
    (`config`/`interrupted`) instead of the `unknown` fallback, which would surface
    as "unclassified provider error" and a `paused: unknown` headline. Matched by
    class name + message so we don't import the SDK's error type (its transport
    surface is not a stable contract). Critically, when a *subagent's* CLI child is
    killed mid-flight its runner's own `interrupt()`/`_cancel` may not have fired,
    so the `_cancel` check alone misses this case."""
    return (type(exc).__name__ == "CLIConnectionError"
            and "terminated process" in str(exc))


# ── Per-run state captured by tool wrappers and read by the receive loop ─────


@dataclass
class _RunState:
    """Per-run mutable state shared between MCP tool wrappers and the runner.

    The wrappers can't return values to the runner directly — they return
    tool_result content to claude. So they write into this dict, and the
    runner reads it after each yielded message.
    """
    complete_step_called: bool = False
    complete_step_args: dict[str, Any] = field(default_factory=dict)
    runtime_errors: list[str] = field(default_factory=list)
    killed_tool_uses: list[dict[str, Any]] = field(default_factory=list)
    # When a tool raises FatalToolError the wrapper captures (tool, message)
    # here so the main runner loop can yield an `error` event with
    # kind="fatal_tool_error" and bail out instead of letting Claude keep
    # going.
    fatal_tool_error: tuple[str, str] | None = None
    # Set once at runner entry; read by the MCP wrapper to append the spawning
    # task tool_call on the parent's agent chain.
    agent_id: str = ""
    # Runner's event loop, recorded at _run_loop entry. _impl raises on a foreign
    # loop (same-loop topology pin). None disables the check.
    runner_loop: Any = None


# ── Tool registry shimming ───────────────────────────────────────────────────


def _build_tools(invocation: AgentInvocation) -> dict[str, Tool]:
    """Same filtering as anthropic._build_tools, including the bash ->
    bash_output/bash_kill injection; kept in lockstep for parity (codex
    reuses this builder verbatim)."""
    reg = get_registry()
    out: dict[str, Tool] = {}
    check_web_capability(invocation.provider_name, invocation.agent_def)
    for name in invocation.agent_def.tools:
        if name == "complete_step":
            continue
        if name == "sub_agent":
            continue
        if name in WEB_TOOL_NAMES:
            # Reserved native-capability names (§4): never registry-looked-up;
            # routed to the native-injection path in _run_loop.
            continue
        if name == "load_skill":
            # Auto-injected below; tolerate a redundant explicit declaration.
            continue
        if name not in reg:
            raise KeyError(f"agent declares unknown tool {name!r}")
        out[name] = reg[name]
    if "sub_agent" in invocation.agent_def.tools:
        out["sub_agent"] = make_task_tool(invocation.tiers)
    out["complete_step"] = make_complete_step_tool(invocation.agent_def.output_schema)
    out["load_skill"] = make_load_skill_tool()
    if "bash" in invocation.agent_def.tools:
        # Auto-granted with bash (same block as anthropic/openai
        # _build_tools): run_in_background is a bash PARAMETER, so an agent
        # with bash but no read/kill tool is a trap — a backgrounded process
        # it can never see or stop. The capability set must be atomic.
        # allowed_tools picks these up automatically (mcp__imp__ prefix is
        # derived from this dict in _build_options).
        out["bash_output"] = reg["bash_output"]
        out["bash_kill"] = reg["bash_kill"]
    return out


def _make_sdk_tools(
    tools: dict[str, Tool], ctx: ToolContext, state: _RunState,
) -> list[Any]:
    """Wrap each imp Tool as an SDK MCP tool callable.

    Each wrapper has the same shape:
      - input: the tool's input_schema applied by the SDK
      - body:  invoke the imp Tool (or dispatch_task / capture complete_step)
      - return: `{"content": [{"type": "text", "text": <result-or-error>}]}`

    `claude_agent_sdk.tool` is the decorator factory. We can't decorate at
    class scope because the tool list is computed per-invocation; instead we
    build them imperatively here.
    """
    from claude_agent_sdk import tool as sdk_tool

    out: list[Any] = []
    for name, t in tools.items():
        out.append(_wrap_one(name, t, ctx, state, sdk_tool))
    return out


def _python_type_for_property(prop_schema: dict[str, Any]) -> Any:
    """Walk a JSON-schema property dict to a Python type for the SDK
    decorator's schema arg. The SDK uses {param_name: python_type} as
    documented in Spike 7 (`@tool("record_word", "...", {"word": str})`).

    Lossy by design: enums collapse to `str`, nested objects to `dict`,
    arrays to `list`. The model may emit an out-of-enum string; imp's own
    `validate_agent_output` catches that downstream — we don't need the
    constraint enforced at the SDK layer.
    """
    t = prop_schema.get("type")
    if isinstance(t, list):
        # Nullable form (`["string", "null"]`): use the non-null type
        t = next((x for x in t if x != "null"), "string")
    mapping: dict[str, Any] = {
        "string": str, "integer": int, "number": float, "boolean": bool,
        "object": dict, "array": list,
    }
    return mapping.get(t, str) if isinstance(t, str) else str  # default str


def _python_schema_for_tool(t: Tool) -> dict[str, Any]:
    """Lossy fallback: {param_name: python_type} for tools whose JSON Schema
    we can't pass through. Currently unused — claude_agent_sdk.tool accepts
    the full JSON Schema dict, so we prefer that path (see _wrap_one).
    """
    props = t.input_schema.get("properties") or {}
    return {name: _python_type_for_property(spec) for name, spec in props.items()}


# Bypass posture for the claude_code provider: the imp tools are exposed through a
# strict allowlist (allowed_tools=[mcp__imp__…], strict_mcp_config=True in
# _build_options), so a task readOnlyHint cannot grant read-only auto-approval
# beyond imp's own permission layer. The hint below is DERIVED from this flag so a
# future interactive/approval mode that flips it drops the hint automatically
# (design §2 honesty note); a test pins the derivation.
CLAUDE_CODE_ALLOWLIST_MODE = True


def _task_annotations() -> Any | None:
    """ToolAnnotations(readOnlyHint=True) for the task tool ONLY, and only when the
    allowlist posture is on AND the width knob permits parallelism. readOnlyHint is
    not literally true; its only operative effect here is unlocking the CLI's MCP
    parallel scheduling. Width gate: IMP_MAX_PARALLEL_TASKS=1 drops the hint
    (Decision 5 kill switch)."""
    from mcp.types import ToolAnnotations
    if CLAUDE_CODE_ALLOWLIST_MODE and max_parallel_tasks() > 1:
        return ToolAnnotations(readOnlyHint=True)
    return None


def _readonly_annotations() -> Any | None:
    """ToolAnnotations(readOnlyHint=True) for a read_only tool. The hint is
    literally true, so it needs NO posture gate (Part B gating: width-only).
    IMP_MAX_PARALLEL_TASKS=1 drops it — the single serialize kill switch."""
    from mcp.types import ToolAnnotations
    if max_parallel_tasks() > 1:
        return ToolAnnotations(readOnlyHint=True)
    return None


def _wrap_one(
    name: str, t: Tool, ctx: ToolContext, state: _RunState, sdk_tool: Any,
) -> Any:
    """Build the SDK tool wrapper for a single imp Tool.

    The wrapper closes over (ctx, state). It runs in the same process as the
    runner; the MCP transport is stdio between claude-p and us, but tool
    invocation crosses it as JSONRPC — the body is async Python.
    """
    # Pass the FULL JSON Schema (with enum, integer-vs-string, required, etc.)
    # to claude_agent_sdk.tool. Per its docs, input_schema accepts a JSON
    # Schema dict for full validation. The previous python-type-only path
    # silently stripped enum + numeric type constraints, which let the LLM
    # emit out-of-enum strings and quoted-string numbers that crashed tools
    # downstream (V1 dogfood Bugs 3 + 4).
    schema: Any = t.input_schema

    async def _impl(args: dict[str, Any]) -> dict[str, Any]:
        if (state.runner_loop is not None
                and asyncio.get_running_loop() is not state.runner_loop):
            raise RuntimeError(
                "claude_code MCP tool dispatched off the runner loop "
                "(same-loop topology pin)")
        try:
            # Decision 10 sweep site: EVERY tool dispatch — the claude_code
            # analog of agents/dispatch.py's first-statement sweep. Inside the
            # try: a sweep raise must degrade to an is_error tool result, not
            # crash the SDK handler. Module-attr call so tests monkeypatch
            # bg._sweep_bg once for all sites.
            await _bg._sweep_bg(ctx)
            if name == "sub_agent":
                # Append the task tool_call event INSIDE _impl. This is race-free:
                # the receive loop processes AssistantMessage on a different
                # async path and would create a duplicate if it also appended
                # here. The receive loop SKIPS task tool_calls (see below).
                #
                # Block id: mint our OWN uuid for the call+result PAIR rather than
                # reuse the SDK's __tool_use_id__ (which is "" here) or the
                # tool_call EVENT id (tc_ev["id"], the wrong id-space — narrative
                # closes a fold by BLOCK id, not event id). A non-empty block id on
                # BOTH the call and the matching result lets narrative's
                # _h_tool_result close the fold deterministically WITH the outcome.
                # This is event-log id-space ONLY; the SDK never sees `minted`.
                minted = uuid.uuid4().hex
                tc_event_id = ""
                # ctx.event_log is always set by _run_loop before tools dispatch;
                # this guard is defensive (no-log → tool_call_event_id="" →
                # subagent agent_id="sub:" with a broken parent link).
                if ctx.event_log is not None:
                    task_input: dict[str, Any] = {
                        "subagent_name": args["subagent_name"],
                        "task_description": args["task_description"],
                    }
                    if args.get("tier"):
                        task_input["tier"] = args["tier"]
                    # Log the call BEFORE dispatch_task — the subagent's parent
                    # link (agent_id="sub:" + this event id) depends on it.
                    sub_def = ctx.subagent_defs.get(args["subagent_name"])
                    label = sub_def.label if sub_def is not None else None
                    # Stamp the call block-tracked (id_ns="block"), matching the
                    # outcome-carrying result below and dispatch.py: the narrative
                    # reads awaits_result from the CALL, so an unstamped call leaves
                    # a backgrounded fold open to early resume-close (frozen end_ts).
                    tc_ev = ctx.event_log.append_tool_call(
                        agent_id=state.agent_id,
                        block=ToolUseBlock(id=minted, name="sub_agent", input=task_input),
                        label=label,
                        id_ns="block")
                    tc_event_id = tc_ev["id"]
                try:
                    result = await dispatch_task(
                        ctx, args["subagent_name"], args["task_description"],
                        tool_call_event_id=tc_event_id,
                        tier=args.get("tier"),
                    )
                except BaseException as exc:
                    # A batched child cannot pause the run. Close the fold with the
                    # SAME minted id (deterministic, outcome-carrying close) and
                    # re-raise; the outer except in _impl degrades it to an
                    # is_error tool_response (a clean unwind through the SDK loop is
                    # not reachable, so a child suspension becomes an is_error result
                    # — the run is not paused). A SubagentSuspended-origin degrade is
                    # marked kind="interrupted" so the fold renders (interrupted).
                    if ctx.event_log is not None:
                        err_text = f"{type(exc).__name__}: {exc}"
                        kind = "interrupted" if isinstance(exc, SubagentSuspended) else None
                        ctx.event_log.append_tool_result(
                            agent_id=state.agent_id,
                            block=ToolResultBlock(minted, err_text, is_error=True),
                            id_ns="block", kind=kind)
                    raise
                if ctx.event_log is not None:
                    # Success close: the canonical, outcome-carrying tool_result for
                    # this task, keyed on the SAME minted block id as the call. The
                    # receive-loop user branch SUPPRESSES the SDK's duplicate task
                    # result so this minted one stays canonical.
                    ctx.event_log.append_tool_result(
                        agent_id=state.agent_id,
                        block=ToolResultBlock(minted, result, is_error=False),
                        id_ns="block")
                return _tool_response(result, is_error=False)
            if name == "complete_step":
                # Capture the args; return a "success" body so claude proceeds
                # to ResultMessage cleanly. The runner sees the captured args
                # after the next yielded message and emits agent_output.
                state.complete_step_called = True
                state.complete_step_args = args
                return _tool_response("step complete", is_error=False)
            raw = await t.async_invoke(ctx, args)
            content_str = raw if isinstance(raw, str) else json.dumps(raw, default=str)
            if name == "bash":
                try:
                    parsed = json.loads(content_str)
                except (TypeError, ValueError):
                    parsed = {}
                rc = parsed.get("returncode")
                if isinstance(rc, int) and rc < 0:
                    state.killed_tool_uses.append({
                        "id": args.get("__tool_use_id__", ""),
                        "name": "bash",
                        "signal": -rc,
                    })
            return _tool_response(content_str, is_error=False)
        except FatalToolError as fte:
            state.fatal_tool_error = (name, str(fte))
            return _tool_response(str(fte), is_error=True)
        except Exception as exc:
            # Decision #6: re-raise a subagent-origin pause for honesty, but a
            # clean synchronous unwind through the claude_agent_sdk loop is NOT
            # reachable — the SDK owns the conversation and a tool callback
            # raising does not propagate to _run_loop as a Python exception. In
            # practice a subagent-origin pause under claude_code degrades to an
            # is_error tool_result. Acceptable: resume = restart the subtree, and
            # subagents are restart-safe by design.
            raise_if_suspended(exc)
            text = f"{type(exc).__name__}: {exc}"
            state.runtime_errors.append(text)
            return _tool_response(text, is_error=True)

    # task: posture+width gated (Decision 5 / §2). read_only tools: width-only
    # honest readOnlyHint. Everything else wraps unannotated.
    if name == "sub_agent":
        annotations = _task_annotations()
    elif t.read_only:
        annotations = _readonly_annotations()
    else:
        annotations = None
    return sdk_tool(name, t.description, schema, annotations=annotations)(_impl)


async def _make_impty_sdk_tools(driver: Any) -> list[Any]:
    """Build SDK MCP tools for each ImptyDriver method, sourced from impty_tool_specs."""
    from claude_agent_sdk import tool as sdk_tool

    from impty.mcp import impty_tool_specs

    specs = await impty_tool_specs(driver)
    return [_wrap_impty_one(spec, sdk_tool) for spec in specs]


def _wrap_impty_one(spec: Any, sdk_tool: Any) -> Any:
    allowed = set(spec.input_schema.get("properties", {}))

    async def _impl(args: dict[str, Any]) -> dict[str, Any]:
        # Strip SDK-injected extras (e.g. __tool_use_id__); the driver methods take
        # only their declared params. Offload the blocking pty work to a thread so
        # the runner event loop is never stalled.
        clean = {k: v for k, v in args.items() if k in allowed}
        try:
            result = await asyncio.to_thread(lambda: spec.method(**clean))
            return _tool_response(result, is_error=False)
        except Exception as exc:
            return _tool_response(str(exc), is_error=True)

    return sdk_tool(spec.name, spec.description, spec.input_schema)(_impl)


def _tool_response(text: str, *, is_error: bool) -> dict[str, Any]:
    """Per SDK convention, tool wrappers return {content: [...]} or
    {content: [...], isError: true}."""
    body: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        body["isError"] = True
    return body


# ── History replay stream ────────────────────────────────────────────────────


def _stream_history(messages: list[Message]) -> Any:
    """Build an async iterable of stream-json dicts from imp messages.

    Each yielded dict is exactly the shape claude-p expects on its stream-json
    stdin (Spike 7 verified). The SDK forwards each dict as one JSONL line
    without transformation.

    Per spec §6.11, replayed `tool_use` blocks are NOT re-dispatched — claude
    treats `assistant` / `system` messages in stream-json input as conversation
    *context*, not as instructions to act on.

    Streaming chunks from claude-p (thinking, then text, then tool_use) land
    in transcript.jsonl as separate assistant messages — the persistence path
    runs once per yielded SDK message. The Anthropic API rejects consecutive
    same-role turns ("tool use concurrency issues"), so we fold streamed
    fragments back into one message per logical turn before replay. This applies
    to BOTH roles: an interrupted turn can leave consecutive *user* messages too
    (a tool_result, then claude-p's "[Request interrupted by user]" marker, then
    imp's complete_step nudge — see change 2026-06-06-06x3ml), and replaying
    three user turns in a row trips the same 400.

    Drop empty assistant content (thinking-only fragments after we extracted
    the thinking blocks) since "assistant with no content" is also invalid.
    """
    return _AsyncIter(_history_dicts(_merge_assistant_fragments(messages)))


def _merge_assistant_fragments(messages: list[Message]) -> list[Message]:
    out: list[Message] = []
    for m in messages:
        if m.role == "assistant" and not m.content:
            continue
        if out and out[-1].role == m.role and m.role in ("assistant", "user"):
            out[-1] = Message(
                role=m.role,
                content=out[-1].content + m.content,
            )
        else:
            out.append(m)
    return out


def _history_dicts(messages: list[Message]) -> Any:
    """Yield one stream-json dict per message in transcript order.

    The trailing message determines whether claude needs a fresh trigger:
    if the last message is a user turn carrying tool_results, claude
    proceeds to the next assistant turn automatically; if it's an assistant
    text-only turn (a resume state ending on assistant prose), the runner
    has to send a fresh user prompt first (handled in the runner above the
    stream).
    """
    session_id = "imp"
    for m in messages:
        if m.role == "system":
            yield {
                "type": "system",
                "session_id": session_id,
                "uuid": str(uuid.uuid4()),
                "message": {"role": "system", "content": _join_text(m.content)},
            }
        elif m.role == "assistant":
            yield {
                "type": "assistant",
                "session_id": session_id,
                "parent_tool_use_id": None,
                "uuid": str(uuid.uuid4()),
                "message": {
                    "id": f"msg_{uuid.uuid4().hex[:12]}",
                    "type": "message",
                    "role": "assistant",
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "content": [_block_to_dict(b) for b in m.content],
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            }
        else:  # user
            yield {
                "type": "user",
                "session_id": session_id,
                "parent_tool_use_id": None,
                "uuid": str(uuid.uuid4()),
                "message": {
                    "role": "user",
                    "content": [_block_to_dict(b) for b in m.content]
                    if any(not isinstance(b, TextBlock) for b in m.content)
                    else _join_text(m.content),
                },
            }


def _block_to_dict(b: Any) -> dict[str, Any]:
    if isinstance(b, TextBlock):
        return {"type": "text", "text": b.text}
    if isinstance(b, ToolUseBlock):
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    return {
        "type": "tool_result", "tool_use_id": b.tool_use_id,
        "content": b.content, "is_error": b.is_error,
    }


_MCP_PREFIX = "mcp__imp__"


def _canon_tool_name(name: str) -> str:
    """Strip the MCP namespace from a tool name before it goes into
    events.jsonl / event payloads. Claude exposes our tools as
    ``mcp__imp__<name>``; Anthropic and OpenAI just use ``<name>``. Strip
    here so the event log and downstream consumers (renderer, tests, users
    reading `imp log`) see a single canonical name across providers.

    Internal SDK-correlation (``b.name`` round-trip back to claude in
    ``_block_to_dict``) keeps the prefix — that's what claude expects on
    history replay."""
    return name[len(_MCP_PREFIX):] if name.startswith(_MCP_PREFIX) else name


def _join_text(content: tuple[Any, ...]) -> str:
    return "".join(b.text for b in content if isinstance(b, TextBlock))


def _serialize_claude_code_message(sdk_msg: Any) -> dict[str, Any]:
    """Serialize an SDK AssistantMessage to llm_response.raw. claude_code has
    no interceptable per-turn API call, so this is a normalized view: canonical
    tool names, thinking preserved, best-effort usage/stop_reason."""
    content: list[dict[str, Any]] = []
    for b in getattr(sdk_msg, "content", []):
        btype = type(b).__name__
        if btype == "TextBlock":
            content.append({"type": "text", "text": b.text})
        elif btype == "ToolUseBlock":
            content.append({"type": "tool_use", "id": b.id,
                            "name": _canon_tool_name(b.name),
                            "input": getattr(b, "input", {})})
        elif btype == "ThinkingBlock":
            content.append({"type": "thinking",
                            "thinking": getattr(b, "thinking", "")})
        else:
            content.append({"type": btype})
    usage = getattr(sdk_msg, "usage", None)
    return {
        "content": content,
        "stop_reason": getattr(sdk_msg, "stop_reason", None),
        "usage": usage if isinstance(usage, dict) else {},
    }


def _parse_claude_code_message(raw: dict[str, Any]) -> Message:
    content: list[Any] = []
    for block in raw.get("content", []):
        kind = block.get("type")
        if kind == "text":
            content.append(TextBlock(text=block.get("text", "")))
        elif kind == "tool_use":
            content.append(ToolUseBlock(id=block["id"], name=block["name"],
                                        input=block.get("input", {})))
    return Message(role="assistant", content=tuple(content))


def _extract_claude_code_diagnostics(raw: dict[str, Any]) -> Diagnostics:
    warnings: list[str] = []
    thinkings: list[str] = []
    for block in raw.get("content", []):
        kind = block.get("type")
        if kind in ("text", "tool_use"):
            continue
        if kind == "thinking":
            thinkings.append(str(block.get("thinking", "") or ""))
        else:
            warnings.append(f"skipped unknown block type {kind!r}")
    return Diagnostics(block_warnings=tuple(warnings), thinkings=tuple(thinkings))


def _parse_cc_web_citations(
    norm_name: str, call_input: dict[str, Any], content: Any,
) -> list[dict[str, Any]]:
    """Parse citations from a CC web builtin result.

    web_search: content is a string ending in `Links: [{"title","url"}, …]`.
    web_fetch:  citation URL comes from the call's `url` input; the content is
                the CLI's extracted summary (no structured links).

    Known soft-failure (acceptable for an audit-only footer): we take the LAST
    `Links:` marker via rfind, so fetched page text that itself contains a later
    `Links:` substring can mislead the parser into [] or garbage. The format is
    verification-gated in Step 1; this is a documented limitation, not a bug."""
    if norm_name == "web_fetch":
        url = call_input.get("url")
        return [{"url": url}] if url else []
    text = content if isinstance(content, str) else ""
    marker = "Links:"
    idx = text.rfind(marker)
    if idx == -1:
        return []
    try:
        arr = json.loads(text[idx + len(marker):].strip())
    except (json.JSONDecodeError, ValueError):
        return []
    cites: list[dict[str, Any]] = []
    for item in arr if isinstance(arr, list) else []:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not url:
            continue
        c: dict[str, Any] = {"url": url}
        if item.get("title"):
            c["title"] = item["title"]
        cites.append(c)
    return cites


class _AsyncIter:
    """Wrap a sync generator as an async iterator for SDK consumption."""

    def __init__(self, gen: Any) -> None:
        self._gen = gen

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._gen)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


# ── SDK message translation ──────────────────────────────────────────────────


def _sdk_message_to_imp(
    sdk_msg: Any,
) -> tuple[Message | None, list[str], list[str]]:
    """Convert an SDK AssistantMessage / UserMessage / SystemMessage to imp.

    Returns (Message, warnings, thinkings). For SDK message types we don't
    translate (ResultMessage, StreamEvent, RateLimitEvent), returns
    (None, [], []).

    ThinkingBlock contents are collected separately so the runner can emit
    `thinking` events for the audit trail. Other unknown content block types
    still become warnings — same contract as the Anthropic and OpenAI runners.
    """
    cls_name = type(sdk_msg).__name__
    if cls_name == "AssistantMessage":
        blocks: list[Any] = []
        warnings: list[str] = []
        thinkings: list[str] = []
        for b in sdk_msg.content:
            btype = type(b).__name__
            if btype == "TextBlock":
                blocks.append(TextBlock(text=b.text))
            elif btype == "ToolUseBlock":
                blocks.append(ToolUseBlock(
                    id=b.id, name=b.name, input=getattr(b, "input", {}),
                ))
            elif btype == "ThinkingBlock":
                thinkings.append(str(getattr(b, "thinking", "") or ""))
            else:
                warnings.append(f"skipped unknown SDK block type {btype!r}")
        return Message(role="assistant", content=tuple(blocks)), warnings, thinkings
    if cls_name == "UserMessage":
        blocks = []
        warnings = []
        for b in sdk_msg.content:
            btype = type(b).__name__
            if btype == "ToolResultBlock":
                blocks.append(ToolResultBlock(
                    tool_use_id=b.tool_use_id, content=b.content,
                    is_error=getattr(b, "is_error", False),
                ))
            elif btype == "TextBlock":
                blocks.append(TextBlock(text=b.text))
            else:
                warnings.append(f"skipped unknown SDK block type {btype!r}")
        return Message(role="user", content=tuple(blocks)), warnings, []
    if cls_name == "SystemMessage":
        # Skip; system messages from claude are init-banner type metadata.
        return None, [], []
    return None, [], []


async def _context_usage_fields(client: Any) -> dict[str, Any]:
    try:
        cu = await client.get_context_usage()
    except Exception:
        return {}
    if cu is None:
        return {}

    def _field(obj: Any, name: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    return {
        "context_window": _field(cu, "rawMaxTokens"),
        "context_pct": _field(cu, "percentage"),
        "context_tokens": _field(cu, "totalTokens"),
    }


class ClaudeCodeAgentRunner:
    """Drives `claude -p` via claude_agent_sdk.

    Persistence: per-step transcript.jsonl, identical to the other runners.
    Resume: stream-json history replay via _stream_history (Spike 7 verified).
    Tools: in-process MCP server (`create_sdk_mcp_server`) hosts the imp
           registry; the agent sees them as `mcp__imp__<name>`.
    """

    def __init__(
        self, model_name: str,
        *,
        client_factory: Any = None,
        user_input_fn: Any = None,
        steer_timeout: float = 2.0,
    ) -> None:
        self.model = model_name
        self._client_factory = client_factory
        self._user_input_fn = user_input_fn
        self._last_no_result_diag: dict[str, Any] = {}
        self._cancel = False
        self._ctx: ToolContext | None = None
        # The live SDK client while a turn streams; interrupt() disconnects it to
        # kill the CLI subprocess (the only effect that breaks a stalled
        # receive_response()). None outside an active `async with client_ctx`.
        self._client: Any = None
        self._impty_driver: Any = None
        # Per-step capture of the nested `claude` CLI's stderr. Built in
        # _run_loop from invocation.transcript_dir; wired into the SDK via the
        # `stderr` callback so a CLI exit-1 carries its cause. None until built.
        self._stderr_sink: Any = None
        # ephemeral CLAUDE_CONFIG_DIR for a fresh run; run()'s finally removes it
        self._config_dir: Path | None = None
        # Cooperative-interrupt (steer) state. _steer_requested is the distinct
        # intent flag (separate from _cancel, which keeps meaning hard-cancel /
        # teardown); the receive loop sets _steer_unblocked when the
        # post-interrupt ResultMessage arrives, letting interrupt() return
        # without escalating. _steer_timeout bounds that wait (injectable so the
        # escalation tests drive it deterministically).
        self._steer_requested = False
        self._steer_unblocked = asyncio.Event()
        self._steer_timeout = steer_timeout

    def _build_options(self, invocation: AgentInvocation, mcp_server: Any,
                       tool_names: list[str], impty_server: Any = None,
                       *, session_store: Any = None,
                       session_id: str | None = None,
                       resume: str | None = None,
                       config_dir: Path | None = None) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions, SdkBeta

        from imp.providers.catalog import CLAUDE_CODE, model_info
        info = model_info(CLAUDE_CODE, self.model)
        # ToolSearch is claude-p's internal schema-loader for deferred MCP
        # tools (default mode for any mcp_server). It only returns schemas;
        # it can't read/write/exec, so allowing it doesn't break the
        # strict-MCP-only stance. Disallowing it breaks resume: when claude-p
        # replays an MCP tool_use from history it tries to ToolSearch the
        # schema first, and a denied tool_use shows up at the API as a
        # tool_use without a matching tool_result ("API Error: 400 due to
        # tool use concurrency issues"). Keep ToolSearch out of the deny
        # list while keeping every other built-in denied.
        enabled_web = cc_enabled_web_builtins(invocation.agent_def)
        exempt = {"ToolSearch", *enabled_web}
        disallowed = [t for t in ALL_CLAUDE_CODE_BUILTIN_TOOLS if t not in exempt]
        drivers = invocation.agent_def.drivers
        mcp_servers: dict[str, Any] = {"imp": mcp_server}
        if impty_server is not None:
            mcp_servers["impty"] = impty_server
        # blank external keys so a parent ANTHROPIC_* can't hijack the nested claude's backend auth
        env: dict[str, str] = {
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_AUTH_TOKEN": "",
        }
        extra_args: dict[str, Any]
        if config_dir is not None:
            # isolated home → suppression flags pointless and they starve session_store;
            # the batcher resolves projects_dir from this env
            env["CLAUDE_CONFIG_DIR"] = str(config_dir)
            extra_args = {}
        else:
            # resume/replay keep the flags so they don't write ~/.claude
            env["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] = "1"
            extra_args = {"no-session-persistence": None}
        return ClaudeAgentOptions(
            model=info.cli_model or self.model,
            betas=cast(list[SdkBeta], list(info.betas)),
            stderr=self._stderr_sink.feed if self._stderr_sink is not None else None,
            env=env,
            # Pin the `claude` subprocess to the change's project_root. Without
            # this the SDK launches claude with cwd=None and leaves $PWD as
            # inherited, so claude keys off a stale $PWD and edits/commits the
            # wrong working tree (e.g. imp's own repo when launched from a
            # subdir or via Popen(cwd=...) which doesn't update $PWD).
            cwd=str(invocation.change_context.project_root),
            system_prompt=invocation.system_prompt,
            mcp_servers=mcp_servers,
            allowed_tools=(
                [f"mcp__imp__{n}" for n in tool_names]
                + list(enabled_web)
                + driver_allowed_tools(drivers)
            ),
            disallowed_tools=disallowed,
            setting_sources=[],
            # strict_mcp_config=True blocks servers sourced from .mcp.json / user
            # settings, NOT servers imp adds to mcp_servers. Adding a first-party
            # server (dict entry + mcp__<name>__* allowlist + codex mirror) is not
            # a posture reversal.
            strict_mcp_config=True,
            extra_args=extra_args,
            session_store=session_store,
            session_id=session_id,
            resume=resume,
        )

    def _build_client(self, opts: Any) -> Any:
        """Return an async-context-manager wrapping ClaudeSDKClient(opts).
        Tests inject a factory that returns a FakeClaudeSDKClient."""
        if self._client_factory is not None:
            return self._client_factory(opts)
        from claude_agent_sdk import ClaudeSDKClient

        from imp.providers.claude_code_session import install_claude_session_isolation
        install_claude_session_isolation()
        return ClaudeSDKClient(opts)

    async def run(self, invocation: AgentInvocation) -> AsyncGenerator[Event, None]:
        try:
            try:
                async for ev in self._run_loop(invocation):
                    yield ev
            except perr.ProviderError as exc:
                yield perr.make_error_event(exc)
                return
            except AwaitingUserInput:
                raise  # bare-turn-end / resume-solicit pause unwinds, not classified
            except Exception as exc:
                raise_if_suspended(exc)
                if self._cancel or _is_terminated_process_teardown(exc):
                    # Interrupt fired before the SDK transport raised — or the SDK
                    # tried to write to a CLI child a (process-group) interrupt had
                    # already terminated. Either way it is interrupt teardown, not a
                    # provider failure: classify it as the same `interrupted` pause
                    # the in-loop _cancel check emits, NOT `unknown` (which surfaces
                    # as "unclassified provider error" / `paused: unknown`). The
                    # terminated-process arm is what catches a killed *subagent*
                    # whose runner's own `_cancel` never fired.
                    yield {"type": "error", "payload": {
                        "category": "config",
                        "kind": "interrupted",
                        "message": "runner cancelled by interrupt",
                    }}
                    return
                tail = self._stderr_sink.tail() if self._stderr_sink else None
                yield perr.make_unknown_error_event(
                    exc, detail={"stderr_tail": tail} if tail else None)
                return
        finally:
            if self._ctx is not None:
                # None guard: _ctx is assigned mid-_run_loop; the finally runs
                # with None when _run_loop raises BEFORE the assignment (the
                # error arm yields, then this fires). An aclose before the
                # first pull never enters the body at all. Lives in run()'s
                # OWN finally (not _run_loop's): an abandoned inner generator
                # only GC-finalizes (PEP 525), so _run_loop's finally is not
                # aclose-deterministic.
                from imp.agents.tools.bg import terminate_background_processes
                with contextlib.suppress(Exception):
                    await terminate_background_processes(self._ctx)
            # The client's own `async with` __aexit__ has run (or its generator
            # was abandoned); drop the stale handle so a late interrupt() is a
            # no-op rather than disconnecting a dead client.
            if self._impty_driver is not None:
                with contextlib.suppress(Exception):
                    self._impty_driver.close()
                self._impty_driver = None
            if self._client is not None:
                await _reap_transport_group(self._client)
            self._client = None
            if self._stderr_sink is not None:
                self._stderr_sink.close()
            if self._config_dir is not None:
                shutil.rmtree(self._config_dir, ignore_errors=True)
                self._config_dir = None

    async def _run_loop(self, invocation: AgentInvocation) -> AsyncGenerator[Event, None]:
        from imp.engine.state import JsonlEventLog
        elog = invocation.event_log or JsonlEventLog(invocation.events_path)
        aid = invocation.agent_id or "step:unnamed"
        from imp.providers.stderr_sink import StderrSink
        self._stderr_sink = StderrSink(invocation.transcript_dir / "stderr.log")

        transcript = Transcript(invocation.transcript_dir / "transcript.jsonl")
        tools = _build_tools(invocation)
        ctx = ToolContext(
            project_root=invocation.change_context.project_root,
            events_path=invocation.events_path,
            subagent_defs=invocation.subagent_defs,
            provider_name=invocation.provider_name,
            model_name=invocation.model_name,
            parent_transcript_dir=invocation.transcript_dir,
            agent_id=aid,
            background_enabled=True,  # full bg surface wired: injection + sweeps + teardown
            change_home=change_home_path(invocation.change_context.change_home),
        )
        ctx.change_context = invocation.change_context
        ctx.change_state = invocation.change_state
        ctx.change = invocation.change          # live Change for rename_change
        ctx.spawn_subagent = spawn_subagent_via_registry
        ctx.resolved_tiers = invocation.resolved_tiers
        ctx.subagent_depth = invocation.subagent_depth
        ctx.coordinator = invocation.coordinator
        ctx.tiers = invocation.tiers
        ctx.verify_command = invocation.verify_command
        ctx.non_interactive = invocation.non_interactive
        ctx.sandbox_policy = invocation.sandbox_policy
        ctx.event_log = elog
        if self._user_input_fn is not None:
            ctx.user_input_fn = self._user_input_fn
        self._ctx = ctx

        state = _RunState()
        state.agent_id = aid
        state.runner_loop = asyncio.get_running_loop()
        sdk_tools = _make_sdk_tools(tools, ctx, state)

        # In-process MCP server hosting imp tools. The real SDK call:
        #   from claude_agent_sdk import create_sdk_mcp_server
        #   mcp_server = create_sdk_mcp_server(name="imp", tools=sdk_tools)
        # Tests stub this via _build_client.
        try:
            from claude_agent_sdk import create_sdk_mcp_server
            mcp_server = create_sdk_mcp_server(name="imp", tools=sdk_tools)
        except ImportError:
            mcp_server = None   # fakes don't need a server

        # Second in-process SDK server for the impty driver, when the agent holds
        # it. The per-run ImptyDriver is stored on self so run()'s finally closes
        # the session no matter how the turn ends.
        impty_server = None
        if mcp_server is not None and "impty" in invocation.agent_def.drivers:
            from impty.driver import ImptyDriver
            self._impty_driver = ImptyDriver()
            impty_tools = await _make_impty_sdk_tools(self._impty_driver)
            impty_server = create_sdk_mcp_server(name="impty", tools=impty_tools)

        # Session-store routing. A FRESH run mirrors its clean session into the
        # store (session_id pins the key). A resume drives claude-p's NATIVE
        # session resume when that store holds bytes (resume= materializes from
        # imp's own bytes — faithful, no stream-json placeholder corruption);
        # absent a stored session it falls back to stream-json replay + manifest.
        # resume= and session_id are mutually exclusive in the SDK: native resume
        # passes resume= and leaves session_id unset.
        existing = transcript.load()
        is_fresh = not existing
        store = session_store_for(invocation.transcript_dir)
        skey = session_key(invocation.change_context.project_root, aid)
        # Native resume is sound ONLY when existing's final turn was actually
        # flushed to the store. The SDK mirror batcher flushes solely at a `result`
        # boundary, so the only resume shape whose final turn carries a result is
        # AWAITING_USER (the agent ended its turn on assistant prose — a bare
        # turn-end yield). Every other shape leaves its final turn UNflushed and
        # outside the resume_base slice: SEND_TO_LLM ends on an unflushed user turn;
        # DANGLING_TOOL_USE (and the balance_on_resume flag's path) ends on an
        # unflushed, interrupted assistant tool_use whose id lives in neither
        # claude-p's loaded session nor the slice — so the surgery's tool_result
        # would be orphaned (the unmatched-tool_use 400 this change targets). Those
        # downgrade to stream-json replay, which re-streams the whole balanced
        # history in position.
        native_resume = (
            (not is_fresh)
            and store_has_conversation_body(await store.load(skey))
            and classify_resume(existing) is ResumeState.AWAITING_USER
        )
        # Native resume sends only messages[resume_base:] — the turns appended on
        # THIS entry; claude-p already holds the rest in its loaded session.
        resume_base = len(existing)
        config_dir: Path | None = None
        if is_fresh:
            config_dir = materialize_fresh_config_dir()
            self._config_dir = config_dir
            opt_store, opt_session_id, opt_resume = (
                store, resume_session_id(aid), None)
        elif native_resume:
            opt_store, opt_session_id, opt_resume = (
                store, None, resume_session_id(aid))
        else:
            opt_store, opt_session_id, opt_resume = (None, None, None)
        # Build options only when we'll use the real SDK (mcp_server is not None).
        # When a client_factory is injected (tests), skip building options to
        # avoid constructing ClaudeAgentOptions with mcp_server=None.
        if mcp_server is not None:
            opts = self._build_options(
                invocation, mcp_server, list(tools), impty_server=impty_server,
                session_store=opt_store, session_id=opt_session_id,
                resume=opt_resume, config_dir=config_dir)
        else:
            opts = None

        if existing:
            messages = existing
            tool_calls_used = 0  # per-invocation; never inherit history
            # ids for which repair synthesized placeholders — must not be named succeeded
            dangling_ids: set[str] = set()
            # Mid-tool Ctrl+C (spec §6.15) can leave a dangling tool_use.
            # Persist a synthetic tool_result so the SEND_TO_LLM path has a
            # balanced transcript; emit `tool_result` events so `imp log`
            # shows what the agent sees on the next turn.
            if invocation.balance_on_resume:
                balance_success_on_resume(
                    messages, transcript, elog, aid,
                    wrapper_text=invocation.retry_feedback)
            else:
                synth = repair_dangling_tool_use_on_resume(messages, transcript)
                if synth is not None:
                    for rb in synth:
                        elog.append_tool_result(agent_id=aid, block=rb)
                        dangling_ids.add(rb.tool_use_id)
            resume_state = classify_resume(messages)
            if resume_state is ResumeState.DANGLING_TOOL_USE:
                yield {"type": "error", "payload": {
                    "category": "config",
                    "kind": "transcript_invalid",
                    "message": "transcript ended with dangling tool_use",
                }}
                return
            if resume_state is ResumeState.AWAITING_USER:
                if ctx.user_input_fn is None:
                    yield {"type": "error", "payload": {
                        "category": "config",
                        "kind": "no_user_input_fn",
                        "message": (
                            "AWAITING_USER resume requires user_input_fn; "
                            "none installed by runner"
                        ),
                    }}
                    return
                # A bare turn-end carries no separate question payload: the
                # agent's prose IS the question, already journaled and rendered.
                # Emit an empty prompt.
                elog.append(agent_id=aid, type="awaiting_user_input",
                            payload={"prompt": ""})
                # Queued interjects satisfy the pending yield directly; fall back
                # to user_input_fn only when the queue is empty.
                reply: str = await _resolve_wait_reply(
                    invocation.coordinator, ctx.user_input_fn)
                deliver_reply_on_resume(
                    messages, transcript, elog, aid, reply)
                # Fall through (do NOT return): the reply drives the next LLM
                # turn as the transcript's trailing user message.
            # §2 resume-entry drain: fold pending interjects into the history
            # that feeds _stream_history(messages) on the first query. The SDK
            # is queried with the WHOLE transcript as a stream, so appending
            # user Messages here is sufficient — no separate string fold needed.
            # Guarded for non-AWAITING_USER: the AWAITING_USER join is handled above.
            if resume_state is not ResumeState.AWAITING_USER:
                drain_all_interjects_at_resume(
                    invocation.coordinator, messages, transcript, elog, aid)
            # ckx9f5: name prior tool calls succeeded so stream-json replay's placeholder
            # errors don't trigger redo; skip the balance path (retry feedback stays last).
            if not invocation.balance_on_resume and not native_resume:
                _last_user = next(
                    (m for m in reversed(messages) if m.role == "user"), None)
                _already_manifested = _last_user is not None and any(
                    isinstance(b, TextBlock) and b.text.startswith(RESUME_MANIFEST_HEADER)
                    for b in _last_user.content
                )
                if not _already_manifested:
                    manifest = build_resume_manifest(
                        messages, exclude_tool_use_ids=frozenset(dangling_ids))
                    if manifest is not None:
                        manifest_msg = Message(
                            role="user", content=(TextBlock(text=manifest),))
                        messages.append(manifest_msg)
                        transcript.append(manifest_msg)
                        elog.append_user_message(agent_id=aid, message=manifest_msg)
        else:
            messages = []
            for m in invocation.initial_messages:
                messages.append(m)
                transcript.append(m)
            tool_calls_used = 0
            # Fresh-entry audit scaffolding. Ordered IMMEDIATELY after the
            # transcript append so the "step_context emitted exactly once per
            # agent_id" invariant holds (resume takes the branch above and
            # emits nothing).
            elog.append_step_context(
                agent_id=aid, parent_id_override=invocation.first_event_parent_id,
                provider=invocation.provider_name, model=self.model,
                system_prompt=invocation.system_prompt,
                # Name-only schemas: the in-process MCP server owns the full
                # input schemas; unlike anthropic/openai we don't pass them here.
                tools_schema=[{"name": _canon_tool_name(n)} for n in tools],
                params={"env.CLAUDE_CONFIG_DIR": str(config_dir)},
                max_tool_calls=invocation.max_tool_calls)
            for m in invocation.initial_messages:
                if all(isinstance(b, TextBlock) for b in m.content):
                    elog.append_user_message(agent_id=aid, message=m)

        ceiling = invocation.max_tool_calls  # may be None (no cap)

        # Loop guard (Fix H). claude_code streams ONE logical turn as multiple
        # AssistantMessage objects (thinking, then text, then tool_use) — see
        # _merge_assistant_fragments. Accumulate the whole turn into pending_*
        # and flush EXACTLY ONCE at the ResultMessage boundary (true end-of-turn).
        guard = LoopGuard()
        pending_text = ""
        pending_tools: dict[str, ToolOutcome] = {}
        pending_complete = False
        # Web builtin call ids: id -> (norm_name, call_input). Populated when a
        # WebSearch/WebFetch ToolUseBlock is observed; consumed when the matching
        # ToolResultBlock arrives so citations can be parsed and the result
        # normalized without incrementing tool_calls_used.
        web_calls: dict[str, tuple[str, dict[str, Any]]] = {}

        def _flush_pending() -> TurnRecord | None:
            nonlocal pending_text, pending_tools, pending_complete
            if not pending_text and not pending_tools and not pending_complete:
                return None
            rec = TurnRecord(assistant_text=pending_text,
                             tools=list(pending_tools.values()),
                             has_complete_step=pending_complete)
            pending_text = ""
            pending_tools = {}
            pending_complete = False
            return rec

        client_ctx = self._build_client(opts)
        async with client_ctx as client:
            # Publish the live client so interrupt() (called from the watchdog/
            # pause task) can disconnect it and break a parked receive_response().
            self._client = client
            # FIRST query streams the prepared history; re-queries send a plain
            # next_text string (asymmetry preserved from the pre-restructure
            # code). Each outer iteration runs its OWN receive_response() so a
            # follow-up turn is consumed from a fresh generator (issue 7:
            # receive_response() terminates after the first ResultMessage).
            next_query: Any = _stream_history(
                messages[resume_base:] if native_resume else messages)
            while True:
                await client.query(next_query)
                requery = False

                async for sdk_msg in client.receive_response():
                    if self._cancel:
                        yield {"type": "error", "payload": {
                            "category": "config",
                            "kind": "interrupted",
                            "message": "runner cancelled by interrupt",
                        }}
                        return

                    # Post-API-site analog (Decision 10): one sweep per
                    # received SDK message — the model-API/CLI latency between
                    # messages is the longest registry-blind gap under this
                    # provider. Cancel check stays first (interrupt priority).
                    await _bg._sweep_bg(ctx)

                    cls_name = type(sdk_msg).__name__
                    imp_msg, warns, thinkings = _sdk_message_to_imp(sdk_msg)
                    for content in thinkings:
                        elog.append(agent_id=aid, type="thinking",
                                    payload={"content": content})
                    for w in warns:
                        elog.append(agent_id=aid, type="runner_warning",
                                    payload={"message": w})
                    if imp_msg is not None:
                        messages.append(imp_msg)
                        transcript.append(imp_msg)

                        if imp_msg.role == "assistant":
                            turn_text = "".join(
                                b.text for b in imp_msg.content
                                if isinstance(b, TextBlock))
                            if turn_text:
                                pending_text = (
                                    pending_text + turn_text if pending_text else turn_text)
                            if cls_name == "AssistantMessage":
                                raw = _serialize_claude_code_message(sdk_msg)
                                cu_fields = await _context_usage_fields(client)
                                elog.append_llm_response(
                                    agent_id=aid, provider=invocation.provider_name,
                                    model=self.model, raw=raw,
                                    usage=raw.get("usage") or {},
                                    stop_reason=raw.get("stop_reason"),
                                    context_window=cu_fields.get("context_window"),
                                    context_pct=cu_fields.get("context_pct"),
                                    context_tokens=cu_fields.get("context_tokens"))
                            for b in imp_msg.content:
                                if isinstance(b, ToolUseBlock):
                                    # Web builtins (§5.2): server/CLI-side, already
                                    # executed. Short-circuit the ENTIRE ceiling
                                    # block — emit a normalized tool_call, record
                                    # the id for citation parsing, and do NOT
                                    # increment tool_calls_used.
                                    if b.name in CC_WEB_BUILTIN_TO_NORM:
                                        norm = CC_WEB_BUILTIN_TO_NORM[b.name]
                                        web_calls[b.id] = (norm, b.input)
                                        elog.append_tool_call(
                                            agent_id=aid,
                                            block=ToolUseBlock(id=b.id, name=norm,
                                                               input=b.input))
                                        continue
                                    # Dual-write `complete_step` state at the
                                    # runner level so the test fakes don't need
                                    # to drive the MCP wrapper. In production the
                                    # wrapper *also* sets these fields when the
                                    # SDK invokes it — idempotent write.
                                    if b.name == "complete_step":
                                        state.complete_step_called = True
                                        state.complete_step_args = b.input
                                    # Ceiling guard: observational here. With
                                    # Claude Code we can't prevent the (K+1)th
                                    # tool dispatch — MCP invocation is async
                                    # claude-side, decoupled from this iteration
                                    # loop. The ceiling becomes a *post hoc* fatal
                                    # check after the ResultMessage.
                                    if ceiling is not None and tool_calls_used >= ceiling:
                                        ceil_msg = (
                                            f"tool_call_ceiling_exceeded (ceiling={ceiling})"
                                        )
                                        elog.append_tool_result(
                                            agent_id=aid,
                                            block=ToolResultBlock(b.id, ceil_msg,
                                                                  is_error=True))
                                        continue
                                    tool_calls_used += 1
                                    canon = _canon_tool_name(b.name)
                                    # Accumulate for the loop guard BEFORE the task
                                    # early-continue so task turns are still tracked.
                                    # is_error defaults True until the following
                                    # UserMessage tool_result lands and flips it.
                                    if canon == "complete_step":
                                        pending_complete = True
                                    pending_tools[b.id] = ToolOutcome(
                                        name=canon, tool_use_id=b.id,
                                        input=b.input, is_error=True)
                                    if canon == "sub_agent":
                                        # tool_call for task is appended inside the
                                        # MCP wrapper (_impl) keyed on a minted uuid
                                        # block id (see :356-362), race-free. Don't
                                        # double-append here.
                                        continue
                                    elog.append_tool_call(
                                        agent_id=aid,
                                        block=ToolUseBlock(id=b.id, name=canon,
                                                           input=b.input))
                        elif imp_msg.role == "user":
                            # ToolResultBlocks generated by claude calling our MCP
                            # tools. Append the corresponding tool_result events.
                            for b in imp_msg.content:
                                if isinstance(b, ToolResultBlock):
                                    if b.tool_use_id in web_calls:
                                        norm, call_input = web_calls[b.tool_use_id]
                                        cites = _parse_cc_web_citations(
                                            norm, call_input, b.content)
                                        elog.append_tool_result(
                                            agent_id=aid, block=b, citations=cites)
                                        continue
                                    # SUPPRESS the SDK's duplicate `task` result.
                                    # _impl already appended the canonical
                                    # outcome-carrying result keyed on the minted
                                    # block id; this incoming toolu_… result is the
                                    # SAME logical close in the SDK id-space. Appending
                                    # it too would double the tool_result (and feed
                                    # narrative a second, fold-less close). Identify
                                    # it by its tool_use_id ∈ pending_tools with
                                    # name=="sub_agent" (canon tracked at the tool_use
                                    # site). Still flip is_error on the task outcome
                                    # below so a failed dispatch doesn't leave the
                                    # ToolOutcome is_error=True → LoopGuard
                                    # repeated_failure on identical re-dispatches.
                                    pend = pending_tools.get(b.tool_use_id)
                                    is_task = pend is not None and pend.name == "sub_agent"
                                    if not is_task:
                                        elog.append_tool_result(agent_id=aid, block=b)
                                    if pend is not None:
                                        pend.is_error = b.is_error
                                        pend.result_preview = \
                                            _result_preview(b.content)

                    # Drain any bash kills detected by the MCP wrapper.
                    while state.killed_tool_uses:
                        info = state.killed_tool_uses.pop(0)
                        elog.append(agent_id=aid, type="tool_killed", payload=info)

                    # A tool raised FatalToolError → bail out instead of letting
                    # Claude burn more LLM calls trying to work around a runtime
                    # misconfiguration.
                    if state.fatal_tool_error is not None:
                        tool_name, msg = state.fatal_tool_error
                        yield {"type": "error", "payload": {
                            "category": "config",
                            "kind": "fatal_tool_error",
                            "tool": tool_name,
                            "message": msg,
                        }}
                        return

                    if cls_name == "ResultMessage":
                        if (self._steer_requested and getattr(
                                sdk_msg, "subtype", None)
                                == "error_during_execution"):
                            # Cooperative interrupt landed: the SDK closed this
                            # turn balanced. Unblock the waiting interrupt() and
                            # route into the attended inline-yield instead of
                            # treating the error result as a termination.
                            self._steer_unblocked.set()
                            self._steer_requested = False
                            # Discard the interrupted turn's partial accumulators
                            # so they don't bleed into the next turn's loop-guard.
                            _flush_pending()
                            # Consume the pause: the steer is resolved inside
                            # this live invocation, so the run continues rather
                            # than returning PAUSED.
                            if invocation.coordinator is not None:
                                invocation.coordinator.clear_pause()
                            # Reap leaked subprocesses only AFTER interrupt():
                            # a killed-process tool_result must not race the stop.
                            from imp.agents.tools.bg import terminate_background_processes
                            from imp.agents.tools.context import terminate_running_processes
                            await terminate_running_processes(ctx)
                            await terminate_background_processes(ctx)
                            last_text = ""
                            for prev in reversed(messages):
                                if prev.role == "assistant":
                                    last_text = _text_of(prev)
                                    break
                            steer = build_inline_solicit(
                                user_input_fn=self._user_input_fn,
                                coordinator=invocation.coordinator,
                                event_log=elog, agent_id=aid)
                            # Raises AwaitingUserInput when no human is attached;
                            # the balanced session makes a resume continue with no 400.
                            reply = await steer(last_text)
                            if self._interrupted():
                                return
                            user_msg = Message(
                                role="user",
                                content=(TextBlock(text=reply),))
                            messages.append(user_msg)
                            transcript.append(user_msg)
                            elog.append_user_message(
                                agent_id=aid, message=user_msg)
                            next_query = reply
                            requery = True
                            break

                        # Loop guard: ResultMessage is the true end-of-turn
                        # marker, so flush the accumulated turn here EXACTLY ONCE
                        # (a turn ending in complete_step set pending_complete →
                        # progress → never trips). Observe before the end-of-turn
                        # handling so a detected loop wins over a nudge.
                        flushed = _flush_pending()
                        if flushed is not None:
                            loop_err = guard.observe(flushed)
                            if loop_err is not None:
                                yield perr.make_error_event(
                                    loop_err, kind=getattr(loop_err, "kind", ""))
                                return

                        # End-of-turn:
                        #   a) complete_step captured → emit agent_output, return
                        #   b) nudge via continuation_fn → send another query
                        #   c) no complete_step and no nudge → error
                        if state.complete_step_called:
                            try:
                                validate_agent_output(
                                    state.complete_step_args,
                                    invocation.agent_def.output_schema,
                                )
                            except Exception as ve:
                                yield {"type": "error", "payload": {
                                    "category": "agent_output_missing",
                                    "kind": "output_validation",
                                    "message": str(ve),
                                }}
                                return
                            yield {"type": "agent_output",
                                   "payload": state.complete_step_args}
                            return

                        if (ceiling is not None and tool_calls_used >= ceiling
                                and not state.complete_step_called):
                            yield {"type": "error", "payload": {
                                "category": "config",
                                "kind": "tool_call_ceiling_exceeded",
                                "ceiling": ceiling,
                            }}
                            return

                        last_text = ""
                        # Walk backward through the messages we appended to find
                        # the most recent assistant message — that's where the
                        # terminal CLI message lives.
                        for prev in reversed(messages):
                            if prev.role == "assistant":
                                last_text = _text_of(prev)
                                break

                        # P2: a ResultMessage flagged is_error (or a nonzero
                        # subprocess exit) is a termination, not a benign
                        # missing-complete_step. Report it typed-or-unknown with
                        # every structured field — before the substring fallback
                        # and never through the nudge path.
                        term = _termination_from_result(
                            sdk_msg, last_text,
                            {**_subprocess_diag(client),
                             "stderr_tail": self._stderr_sink.tail()})
                        if term is not None:
                            _roll_back_error_turn(messages, transcript)
                            yield perr.make_error_event(
                                term, kind=getattr(term, "kind", ""))
                            return

                        # Classify before the nudge: a surfaced provider error
                        # (auth/quota) must win over a pointless nudge. This runs
                        # on each nudge turn (up to N+1), not just the terminal one
                        # — intentional, so an auth failure surfaces on turn 1
                        # instead of after N nudges.
                        classified = _classify_agent_text(last_text)
                        if classified is not None:
                            _roll_back_error_turn(messages, transcript)
                            yield perr.make_error_event(
                                classified, kind=getattr(classified, "kind", ""))
                            return

                        cont = invocation.continuation_fn
                        if cont is not None:
                            next_text = cont(last_text)
                            if inspect.isawaitable(next_text):
                                next_text = await next_text
                            if self._interrupted():
                                # The interrupt landed while we were resolving the
                                # continuation. Don't nudge the model to keep going
                                # after a Ctrl-C — pause at the turn boundary.
                                # Without this the nudge + re-query proceeds and the
                                # cancel is only caught later at the no-result
                                # fallthrough, after a wasted round-trip that
                                # surfaces a spurious `network` warning.
                                yield {"type": "error", "payload": {
                                    "category": "config",
                                    "kind": "interrupted",
                                    "message": "runner cancelled by interrupt",
                                }}
                                return
                            if next_text is None:
                                yield {"type": "error", "payload": {
                                    "category": "agent_incomplete",
                                    "kind": "no_complete_step_after_nudges",
                                    "message": "agent did not call complete_step after nudges",
                                    "detail": {"last_agent_text": last_text[:500]},
                                }}
                                return
                            user_msg = Message(
                                role="user", content=(TextBlock(text=next_text),),
                            )
                            messages.append(user_msg)
                            transcript.append(user_msg)
                            elog.append_user_message(agent_id=aid, message=user_msg)
                            # Turn boundary: this is the only point where THIS
                            # runner sets up the next SDK query (the SDK owns the
                            # in-query tool loop). Deliver any queued human
                            # interject as a user message + user_interjected event,
                            # and fold its text into the next query string so the
                            # SDK actually sees it on this re-query. Reached only
                            # after the turn's tool results are flushed, so no
                            # subagent is mid-dispatch — S4 holds.
                            next_query = next_text
                            interject_msg = _drain_interject_message(
                                invocation.coordinator, elog, aid)
                            if interject_msg is not None:
                                messages.append(interject_msg)
                                transcript.append(interject_msg)
                                # Emit user_message THEN user_interjected, uniform
                                # with deliver_interject/codex (issue: order was
                                # reversed here).
                                elog.append_user_message(
                                    agent_id=aid, message=interject_msg)
                                elog.append(
                                    agent_id=aid, type="user_interjected",
                                    payload={"text": _text_of(interject_msg)})
                                next_query = f"{next_text}\n\n{_text_of(interject_msg)}"
                            # Re-query on a FRESH receive_response() (issue 7):
                            # break the inner loop so the outer while restarts.
                            requery = True
                            break

                        yield {"type": "error", "payload": {
                            "category": "agent_output_missing",
                            "kind": "no_complete_step",
                            "message": "agent finished without calling complete_step",
                            "detail": {"last_agent_text": last_text[:500]},
                        }}
                        return

                # INVARIANT: any `break` from the inner async-for that intends to
                # re-query MUST set requery=True first. A bare break falls through
                # to the no-result diagnostic below and emits no_result_message.
                if requery:
                    # A re-query was queued: loop back and run a fresh
                    # receive_response() for the next turn.
                    continue

                # Inner loop drained WITHOUT a re-query and without a
                # ResultMessage-driven return: receive_response() was exhausted
                # with no ResultMessage this turn. Capture subprocess
                # diagnostics here, while the transport is still live (before
                # __aexit__ runs disconnect() and nulls _transport). Against
                # today's SDK SubprocessCLITransport does not expose
                # returncode/stderr_tail, so this returns {}; the capture is
                # forward-compatible for when the SDK adds those attributes.
                self._last_no_result_diag = {
                    **_subprocess_diag(client),
                    "stderr_tail": self._stderr_sink.tail()}
                break

        # Fell out of the async-with block without yielding a ResultMessage.
        # If we were cancelled (Ctrl-C), the subprocess was torn down mid-turn
        # and the re-query's receive_response yields nothing — so the per-message
        # cancel check above never runs. Report the cancellation here too, as
        # `config`/interrupted (a Pause), NOT `network` (a retry): auto-retrying
        # an interrupted turn replays a transcript the API rejects with a 400
        # ("tool use concurrency issues"). See change 2026-06-06-06x3ml.
        if self._cancel:
            yield {"type": "error", "payload": {
                "category": "config",
                "kind": "interrupted",
                "message": "runner cancelled by interrupt",
            }}
            return

        # Otherwise this is a genuine early close. Use the diagnostics captured
        # inside the block (transport was live). Classification stays `network`
        # (retryable).
        diag = self._last_no_result_diag
        message = "SDK closed without yielding a ResultMessage"
        if diag:
            message = f"{message} (exit={diag.get('exit')})"
        yield {"type": "error", "payload": {
            "category": "network",
            "kind": "no_result_message",
            "message": message,
            "detail": diag,
        }}

    def _interrupted(self) -> bool:
        """Read the cancel flag through a call so mypy doesn't narrow it to a
        constant after the per-message guard. interrupt() flips `_cancel` from
        another task, which mypy's flow analysis can't model — without this
        indirection the mid-turn cancel check reads as unreachable."""
        return self._cancel

    async def interrupt(self) -> None:
        # Cooperative tier (the first move): ask the SDK to end the turn
        # balanced — no subprocess kill, no torn transcript. The receive loop
        # sets _steer_unblocked when the post-interrupt ResultMessage arrives.
        # Wait a bounded interval; only a genuinely frozen claude-p (no unblock)
        # falls through to the hard-kill escalation. Fire client.interrupt()
        # BEFORE any process termination, so a killed-process tool_result can
        # never race the interrupt and carry the turn past the stop.
        client = self._client
        if client is not None and hasattr(client, "interrupt"):
            self._steer_requested = True
            # Reset the unblock latch on EVERY interrupt: a prior cooperative
            # steer left it set, and a stale-set Event would make this wait_for
            # return immediately and skip escalation on a SECOND interrupt (a
            # re-steer, or the watchdog firing on a later genuine freeze). Clear
            # it before client.interrupt() so only THIS turn's post-interrupt
            # ResultMessage can satisfy the wait.
            self._steer_unblocked.clear()
            with contextlib.suppress(Exception):
                await client.interrupt()
            try:
                await asyncio.wait_for(
                    self._steer_unblocked.wait(), timeout=self._steer_timeout)
                return
            except TimeoutError:
                self._steer_requested = False
        await self._hard_interrupt()

    async def _hard_interrupt(self) -> None:
        from imp.agents.tools.bg import terminate_background_processes
        from imp.agents.tools.context import terminate_running_processes
        self._cancel = True
        try:
            if self._ctx is not None:
                await terminate_running_processes(self._ctx)
                # Decision 7: pause kills background processes too (leak
                # resistance); the persisted tombstones give the resumed agent an
                # explanation. Idempotent vs the run() finally (both fire on pause).
                await terminate_background_processes(self._ctx)
        finally:
            # disconnect() must fire even if a teardown raises: _cancel is only
            # observed BETWEEN SDK messages, so a stalled agent parked in
            # receive_response() awaiting a message that never comes (the
            # watchdog's exact case) never sees it. Killing the transport
            # subprocess is the only effect that breaks that parked read — the
            # claude_code analog of codex's AbortController.abort(). Without it
            # the CLI child leaks past teardown and races the step's restart.
            # Idempotent vs the `async with` __aexit__ that also disconnects.
            client = self._client
            if client is not None:
                await _reap_transport_group(client)
                with contextlib.suppress(Exception):
                    await client.disconnect()


class ClaudeCodeProvider:
    name = "claude_code"

    def __init__(self, model_name: str, *, client_factory: Any = None) -> None:
        self.model = model_name
        self._client_factory = client_factory

    def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
        return ClaudeCodeAgentRunner(
            model_name=self.model, client_factory=self._client_factory,
            user_input_fn=invocation.user_input_fn,
        )


TIER_DEFAULTS: dict[str, str] = {"low": "haiku", "medium": "sonnet", "high": "opus-1m"}
ENV_VAR: str | None = None  # CLI-authenticated (claude login); no env var


def _env_check() -> bool:
    return shutil.which("claude") is not None


def _factory(model: str) -> Provider:
    return ClaudeCodeProvider(model_name=model)
