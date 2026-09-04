# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
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

History isolation, by config dir. claude-p (CC 2.x) writes an `ai-title` session
stub to its `CLAUDE_CONFIG_DIR`/projects/ on every spawn; imp keeps that off the
real `~/.claude` by controlling where that dir points. Fresh and stream-json
replay both run against an imp-materialized ephemeral `CLAUDE_CONFIG_DIR`
(`materialize_fresh_config_dir`, removed in `run()`'s finally) — a throwaway sink
whose stub is discarded on cleanup. Native resume is the only disposition that
leaves `config_dir = None`; it runs under the SDK's own materialized temp dir.
No disposition writes to the real `~/.claude`. Imp persists its own transcripts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import signal
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from imp.agents.agent_loop import run_agent_loop
from imp.agents.classify import ResumeState, _contains_tool_result, classify_resume
from imp.agents.loop_guard import (
    LoopGuard,
    ToolOutcome,
    TurnRecord,
    _result_preview,
)
from imp.agents.loop_items import EndOfTurn, Error, Thinking, Warning
from imp.agents.parallel import max_parallel_tasks
from imp.agents.runner import (
    AgentInvocation,
    AgentRunner,
    ContentPart,
    Event,
    ImagePart,
    Message,
    TextBlock,
    TextPart,
    ToolResultBlock,
    ToolUseBlock,
    canonicalize_tool_name,
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
from imp.agents.tools.context import (
    ToolContext,
    kill_process_group,
    request_bracketed,
    suspend_inactivity_watchdog,
)
from imp.agents.tools.errors import FatalToolError
from imp.agents.tools.load_skill import make_load_skill_tool
from imp.agents.transcript import (
    RESUME_CONTINUE_NUDGE,
    RESUME_REDO_NUDGE,
    Transcript,
    captured_result_key,
    deliver_continuation_reply,
    deliver_engine_continuation,
    repair_dangling_tool_use_on_resume,
    synthesize_native_success_on_resume,
)
from imp.engine.agent_run import (
    INTERRUPT_STEER_HINT,
    _resolve_wait_reply,
    build_inline_solicit,
    continuation_nudge,
    drain_all_interjects_at_resume,
    last_assistant_text,
    most_recent_assistant_text,
)
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.engine.drivers import driver_allowed_tools
from imp.providers import errors as perr
from imp.providers._cc_builtins import ALL_CLAUDE_CODE_BUILTIN_TOOLS
from imp.providers.base import Provider
from imp.providers.claude_code_config_dir import materialize_fresh_config_dir
from imp.providers.claude_code_session_store import (
    message_to_session_records,
    resume_session_id,
    session_key,
    session_store_for,
)
from imp.providers.claude_token_refresh import (
    FAILED_INVALID_GRANT,
    RefreshOutcome,
    refresh_canonical_token,
)
from imp.providers.observe_classify import (
    _classify_agent_text,
    classifies_as_provider_error,
)
from imp.providers.observe_session import ObserveSession
from imp.providers.resolve import spawn_subagent_via_registry
from imp.providers.web_native import (
    CC_WEB_BUILTIN_TO_NORM,
    WEB_TOOL_NAMES,
    cc_enabled_web_builtins,
    check_web_capability,
)

if TYPE_CHECKING:
    from claude_agent_sdk import EffortLevel, ThinkingConfig


def _read_token_expiry(config_dir: Path | None, now_ms: int) -> dict[str, object]:
    """Best-effort read of the OAuth access-token expiry from the spawn's
    credential file. Never raises: a missing / locked / torn file (the CLI
    writes .credentials.json non-atomically, so a concurrent read can see a torn
    file) yields token_expires_at=None. Only the expiry timestamp is read, never
    token material. expiresAt is epoch milliseconds; the 5-minute buffer matches
    the CLI's own proactive-refresh window, so a token within 5 min of expiry
    already reads as expired."""
    base = config_dir if config_dir is not None else Path.home() / ".claude"
    expires_at: int | None = None
    try:
        data = json.loads((base / ".credentials.json").read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        if isinstance(oauth, dict):
            value = oauth.get("expiresAt")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                expires_at = int(value)
    except (OSError, ValueError):
        expires_at = None
    buffer_ms = 5 * 60 * 1000
    return {
        "token_expires_at": expires_at,
        "token_expired": (None if expires_at is None
                          else expires_at <= now_ms + buffer_ms),
        "checked_at": now_ms,
    }


_DEAD_GRANT_MESSAGE = (
    "claude: OAuth refresh token is dead; re-authentication required")


def _journal_token_refresh(elog: Any, aid: str, outcome: RefreshOutcome) -> None:
    payload: dict[str, Any] = {"outcome": outcome.outcome}
    if outcome.expires_at is not None:
        payload["expires_at"] = outcome.expires_at
    elog.append(agent_id=aid, type="token_refresh", payload=payload)


def _pre_spawn_token_refresh(elog: Any, aid: str) -> None:
    """Refresh the canonical claude token before spawn; a dead token needs a human /login."""
    outcome = refresh_canonical_token(int(time.time() * 1000))
    _journal_token_refresh(elog, aid, outcome)
    if outcome.outcome == FAILED_INVALID_GRANT:
        raise perr.AuthTerminalError(_DEAD_GRANT_MESSAGE)


def _termination_from_result(
    sdk_msg: Any, last_text: str, diag: dict[str, Any],
    config_dir: Path | None = None,
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
    labeled = _classify_agent_text(last_text, anchored=False)
    status = detail["api_error_status"]
    if (isinstance(status, int) and status == 400
            and "tool use concurrency" in last_text.lower()):
        # A transient request-shaping 400 (concurrent tool_use batching). Route
        # to the network category (RetryWithBackoff) so a stray replay-400
        # self-heals instead of terminally pausing. Reassigns unconditionally so a
        # colon-form "API Error: 400 …" — which _classify_agent_text labels a
        # terminal ApiError — is rerouted here too, not left to terminate.
        labeled = perr.NetworkError(
            f"claude_code: tool use concurrency (api_error_status={status})")
    if labeled is None and isinstance(status, int) and 500 <= status < 600:
        labeled = perr.NetworkError(f"claude_code: api_error_status={status}")
    if labeled is None and status in (401, 403):
        labeled = perr.AuthError(f"claude_code: api_error_status={status}")
        labeled.kind = f"http_{status}"  # type: ignore[attr-defined]
    if labeled is None and status == 429:
        labeled = perr.RateLimitError(f"claude_code: api_error_status={status}")
    if isinstance(labeled, (perr.AuthError, perr.AuthTerminalError)):
        detail.update(_read_token_expiry(config_dir, int(time.time() * 1000)))
    if isinstance(labeled, (perr.QuotaError, perr.AuthError, perr.AuthTerminalError,
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
    """Drop the trailing assistant error turn; an earlier one is delivered history."""
    if not messages or messages[-1].role != "assistant":
        return
    del messages[-1]
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


def _tail_has_complete_step(messages: list[Message]) -> bool:
    """True when the transcript's last turn is an assistant turn whose tool_use is
    a `complete_step` awaiting its result.

    On a non-rework resume of this shape (bare backward goto) imp folds a SUCCESS
    result into the loaded session via `synthesize_native_success_on_resume`
    (claude-p's own pairing would inject an `is_error` for the dangling
    `complete_step`, telling the agent its already-accepted work failed) and kicks
    the delta with a redo nudge. On a rework resume the `complete_step` is left
    dangling instead, and the is_error feedback result streams as the delta
    claude-p pairs it against."""
    last = messages[-1] if messages else None
    if last is None or last.role != "assistant":
        return False
    return any(
        isinstance(b, ToolUseBlock) and _canon_tool_name(b.name) == "complete_step"
        for b in last.content
    )


def _drain_interject_message(coordinator: Any, elog: Any,
                             agent_id: str) -> Message | None:
    """claude_code variant of agent_run.deliver_interject. The SDK is queried
    with a STRING, not a messages list, so the caller folds the returned
    message's text into the next query itself — here we only drain the queue,
    returning a user Message (or None when empty). The caller appends it to
    messages/transcript and emits a single `user_message`/`interject` event."""
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
    # Per-tool wall-time (ms) keyed by SDK tool_use_id, written by the MCP
    # wrapper and read once by the receive loop when it appends the tool_result.
    tool_durations: dict[str, float] = field(default_factory=dict)
    # The flush-lock-guarded _flush_llm_group closure, assigned once by _run so
    # the out-of-band _impl sub_agent path can commit accumulated parent prose
    # (as an agent_message) BEFORE it eagerly appends the sub_agent tool_call.
    # None until _run assigns it (pre-loop); _impl guards on that.
    flush_llm_group: Any = None


# ── Tool registry shimming ───────────────────────────────────────────────────


def _cc_thinking_posture(
    tier: str | None,
) -> tuple[ThinkingConfig | None, EffortLevel | None]:
    """Per-tier (thinking, effort) for ClaudeAgentOptions, imp-owned so the
    spawned claude's posture never re-floats to the subscriber/GrowthBook
    default. medium/high run adaptive at effort 'high' (matching the anthropic
    provider); low is thinking-off (haiku supports neither adaptive nor effort);
    an untiered/legacy invocation keeps the claude-p default (no pin)."""
    if tier in ("medium", "high"):
        return {"type": "adaptive", "display": "summarized"}, "high"
    if tier == "low":
        return {"type": "disabled"}, None
    return None, None


def _build_tools(invocation: AgentInvocation) -> dict[str, Tool]:
    """Same filtering as anthropic._build_tools, including the bash ->
    bash_output/bash_kill injection."""
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
    if invocation.subagent_depth == 0:
        out["complete_step"] = make_complete_step_tool(
            invocation.agent_def.output_schema)
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


CAPTURED_TOOL_RESULTS_FILENAME = "captured_tool_results.jsonl"


def _append_captured_tool_result(
    transcript_dir: Path, name: str, tool_input: dict[str, Any], content: str,
) -> None:
    """Best-effort append of one plain-tool success to the per-step sidecar, keyed by
    tool name + input (the SDK gives no usable tool_use_id for in-process MCP tools).
    The whole body is inside suppress: a capture failure must never flip a real
    success into an is_error result — a lost line just falls back to the placeholder.
    """
    path = transcript_dir / CAPTURED_TOOL_RESULTS_FILENAME
    with contextlib.suppress(Exception):
        line = json.dumps(
            {"name": name, "input": tool_input, "content": content, "is_error": False})
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _load_captured_tool_results(
    transcript_dir: Path,
) -> dict[str, tuple[str, bool]]:
    """Load the per-step capture sidecar into a captured_result_key(name, input) ->
    (content, is_error) dict; {} when absent, so repair falls back to placeholders.
    """
    path = transcript_dir / CAPTURED_TOOL_RESULTS_FILENAME
    if not path.exists():
        return {}
    out: dict[str, tuple[str, bool]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        key = captured_result_key(rec["name"], rec["input"])
        out[key] = (rec["content"], rec["is_error"])
    return out


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
                    # Commit any accumulated parent prose as an agent_message BEFORE
                    # this eager tool_call, so narration renders above the sub_agent
                    # fold it introduces (parity with ordinary tools). The flush's
                    # lock makes this wait for any in-flight receive-loop flush's
                    # emit. None until _run assigns the closure (pre-loop).
                    if state.flush_llm_group is not None:
                        await state.flush_llm_group()
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
                # after the next yielded message and emits agent_result.
                state.complete_step_called = True
                state.complete_step_args = args
                return _tool_response("step complete", is_error=False)
            if name == "load_skill" and state.flush_llm_group is not None:
                # Commit any accumulated turn prose as an agent_message BEFORE
                # load_skill appends its skill_load event from inside async_invoke,
                # so the event log lands narration-then-load (parity with the eager
                # sub_agent flush above). No-ops when no llm group is open; None
                # until _run assigns the closure (pre-loop).
                await state.flush_llm_group()
            tuid = args.get("__tool_use_id__", "")
            clean_args = {k: v for k, v in args.items()
                          if not (k.startswith("__") and k.endswith("__"))}
            start = time.perf_counter()
            with suspend_inactivity_watchdog(ctx):
                raw = await t.async_invoke(ctx, clean_args)
            dur_ms = (time.perf_counter() - start) * 1000.0
            if tuid:
                state.tool_durations[tuid] = dur_ms
            if (isinstance(raw, list) and raw
                    and all(isinstance(p, (TextPart, ImagePart)) for p in raw)):
                if ctx.parent_transcript_dir is not None:
                    caption = " ".join(p.text for p in raw if isinstance(p, TextPart))
                    _append_captured_tool_result(
                        ctx.parent_transcript_dir, name, clean_args, caption)
                return _tool_response_parts(raw)
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
            if ctx.parent_transcript_dir is not None:
                _append_captured_tool_result(
                    ctx.parent_transcript_dir, name, clean_args, content_str)
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

    specs = impty_tool_specs(driver)
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


def _tool_response_parts(parts: list[ContentPart]) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for p in parts:
        if isinstance(p, TextPart):
            content.append({"type": "text", "text": p.text})
        else:
            content.append({"type": "image", "data": p.data, "mimeType": p.media_type})
    return {"content": content}


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
    imp's complete_step nudge — see quest 2026-06-06-06x3ml), and replaying
    three user turns in a row trips the same 400.

    Drop empty assistant content (thinking-only fragments after we extracted
    the thinking blocks) since "assistant with no content" is also invalid.

    The fold plus the tool_result regrouping claude's loader needs are both in
    `_project_for_cli`.
    """
    return _AsyncIter(_history_dicts(_project_for_cli(messages)))


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


def _project_for_cli(messages: list[Message]) -> list[Message]:
    """Merge fragments, pair every tool_result behind its tool_use, merge again.

    claude's loader pairs an assistant record's tool_use only with the record
    immediately after it (extras dropped, gaps filled with an error stub), and
    the closing merge undoes the split regrouping causes — consecutive same-role
    turns are the 400 `_stream_history` documents. A tool_result whose tool_use
    is absent is paired across the resume_base split; it is kept, never dropped.
    """
    merged = _merge_assistant_fragments(messages)
    results: dict[str, ToolResultBlock] = {}
    issued: set[str] = set()
    for m in merged:
        for b in m.content:
            if isinstance(b, ToolResultBlock):
                results.setdefault(b.tool_use_id, b)
            elif isinstance(b, ToolUseBlock):
                issued.add(b.id)
    out: list[Message] = []
    for m in merged:
        if m.role == "assistant":
            out.append(m)
            paired = tuple(
                results[b.id] for b in m.content
                if isinstance(b, ToolUseBlock) and b.id in results)
            if paired:
                out.append(Message(role="user", content=paired))
        elif m.role == "user":
            kept = [b for b in m.content
                    if not (isinstance(b, ToolResultBlock) and b.tool_use_id in issued)]
            if kept:
                kept.sort(key=lambda b: not isinstance(b, ToolResultBlock))
                out.append(Message(role="user", content=tuple(kept)))
        else:
            out.append(m)
    return _merge_assistant_fragments(out)


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
                    # >1 block → block list keeps merged text turns (interjects) separate.
                    "content": [_block_to_dict(b) for b in m.content]
                    if len(m.content) > 1
                    or any(not isinstance(b, TextBlock) for b in m.content)
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


def _canon_tool_name(name: str) -> str:
    """Strip the MCP namespace from a tool name before it goes into
    events.jsonl / event payloads. Claude exposes our tools as
    ``mcp__imp__<name>``; Anthropic and OpenAI just use ``<name>``. Strip
    here so the event log and downstream consumers (renderer, tests, users
    reading `imp log`) see a single canonical name across providers.

    Delegates to the shared ``canonicalize_tool_name`` (imp.agents.runner) —
    the single source of truth so resume-shape classification and this
    event-name canonicalization can never drift apart.

    Internal SDK-correlation (``b.name`` round-trip back to claude in
    ``_block_to_dict``) keeps the prefix — that's what claude expects on
    history replay."""
    return canonicalize_tool_name(name)


def _join_text(content: tuple[Any, ...]) -> str:
    return "".join(b.text for b in content if isinstance(b, TextBlock))


def _serialize_claude_code_message(sdk_msg: Any) -> dict[str, Any]:
    """Serialize an SDK AssistantMessage to agent_message.raw. claude_code has
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


_NO_RESULT_SENTINEL = object()


class ClaudeCodeSession(ObserveSession):
    """The observe `Session` claude_code's `run()` drives via `run_agent_loop`.

    Each hook body is a relocated region of the old `_run_loop`, delegating to
    the surviving module-level helpers. `_enter` builds ctx/tools/client/state and
    the fresh-vs-resume disposition; `_receive` owns the query/receive_response
    loop plus the mid-turn steer/handoff/complete cooperative-interrupt machinery;
    `_message_to_items`/`_is_terminal` shape the per-message appends, the
    complete_step capture, and the ResultMessage terminal decision. The terminal
    outcome (validate / nudge / ceiling / subagent / continuation) is owned by the
    shared driver.
    """

    # complete_step is captured race-free by the MCP wrapper into runner._state.
    @property
    def _complete_step_called(self) -> bool:
        st = self.runner._state
        return bool(st and st.complete_step_called)

    @property
    def _complete_step_args(self) -> dict[str, Any]:
        st = self.runner._state
        return st.complete_step_args if st is not None else {}

    async def _enter(self) -> None:
        invocation = self.invocation
        runner = self.runner
        from imp.engine.state import JsonlEventLog
        elog = invocation.event_log or JsonlEventLog(invocation.events_path)
        aid = invocation.agent_id or "step:unnamed"
        self.elog = elog
        self.aid = aid
        self.subagent_depth = invocation.subagent_depth
        from imp.providers.stderr_sink import build_stderr_sink
        runner._stderr_sink = build_stderr_sink(
            invocation.transcript_dir / "stderr.log",
            elog=elog, aid=aid, source="claude")

        transcript = Transcript(invocation.transcript_dir / "transcript.jsonl")
        self.transcript = transcript
        tools = _build_tools(invocation)
        self.tools = tools
        ctx = ToolContext(
            project_root=invocation.quest_context.project_root,
            events_path=invocation.events_path,
            subagent_defs=invocation.subagent_defs,
            provider_name=invocation.provider_name,
            model_name=invocation.model_name,
            parent_transcript_dir=invocation.transcript_dir,
            agent_id=aid,
            background_enabled=True,  # full bg surface wired: injection + sweeps + teardown
            quest_home=invocation.quest_context.quest_dir,
        )
        ctx.quest_context = invocation.quest_context
        ctx.quest_state = invocation.quest_state
        ctx.quest = invocation.quest          # live quest for rename_quest
        ctx.spawn_subagent = spawn_subagent_via_registry
        ctx.resolved_tiers = invocation.resolved_tiers
        ctx.subagent_depth = invocation.subagent_depth
        ctx.coordinator = invocation.coordinator
        ctx.tiers = invocation.tiers
        ctx.test_gates = invocation.test_gates
        ctx.pythonpath = invocation.pythonpath
        ctx.undecided = invocation.undecided
        ctx.non_interactive = invocation.headless
        ctx.base_sandbox_policy = invocation.sandbox_policy
        ctx.event_log = elog
        if runner._user_input_fn is not None:
            ctx.user_input_fn = runner._user_input_fn
        self.ctx = ctx
        runner._ctx = ctx

        state = _RunState()
        state.agent_id = aid
        state.runner_loop = asyncio.get_running_loop()
        runner._state = state
        sdk_tools = _make_sdk_tools(tools, ctx, state)

        try:
            from claude_agent_sdk import create_sdk_mcp_server
            mcp_server = create_sdk_mcp_server(name="imp", tools=sdk_tools)
        except ImportError:
            mcp_server = None   # fakes don't need a server

        impty_server = None
        if mcp_server is not None and "impty" in invocation.agent_def.drivers:
            from impty.driver import ImptyDriver
            runner._impty_driver = ImptyDriver()
            impty_tools = await _make_impty_sdk_tools(runner._impty_driver)
            impty_server = create_sdk_mcp_server(name="impty", tools=impty_tools)

        existing = transcript.load(
            ctx.quest_home / "blobs" if ctx.quest_home is not None else None)
        is_fresh = not existing
        # A subagent's done-signal is a bare-turn-end terminal (assistant prose,
        # no complete_step): re-emit the final text as the deliverable rather than
        # native-resuming or soliciting the human (AWAITING_USER).
        if (invocation.subagent_depth > 0 and not is_fresh
                and classify_resume(existing) is ResumeState.AWAITING_USER):
            self.messages = existing
            self.ceiling = invocation.max_tool_calls
            self.guard = LoopGuard(interactive=invocation.interactive)
            self._pending_terminal = EndOfTurn(
                last_text=last_assistant_text(existing))
            return
        store = session_store_for(invocation.transcript_dir)
        skey = session_key(invocation.quest_context.project_root, aid)
        shape_native_eligible = (
            (not is_fresh)
            and classify_resume(existing) in (
                ResumeState.AWAITING_USER, ResumeState.SEND_TO_LLM,
                ResumeState.DANGLING_TOOL_USE, ResumeState.STEP_COMPLETED)
        )
        resume_base = len(existing)
        if (shape_native_eligible
                and classify_resume(existing) is ResumeState.SEND_TO_LLM):
            while (resume_base > 0
                   and existing[resume_base - 1].role == "user"
                   and not _contains_tool_result(existing[resume_base - 1])):
                resume_base -= 1
        fresh_stream = shape_native_eligible and resume_base == 0
        native_resume = shape_native_eligible
        _pre_spawn_token_refresh(elog, aid)
        config_dir: Path | None = None
        if is_fresh or fresh_stream:
            config_dir = materialize_fresh_config_dir()
            runner._config_dir = config_dir
            opt_store, opt_session_id, opt_resume = (
                store, resume_session_id(aid), None)
        elif native_resume:
            opt_store, opt_session_id, opt_resume = (
                store, None, resume_session_id(aid))
        if mcp_server is not None:
            opts = self.runner._build_options(
                invocation, mcp_server, list(tools), impty_server=impty_server,
                session_store=opt_store, session_id=opt_session_id,
                resume=opt_resume, config_dir=config_dir)
        else:
            opts = None

        if existing:
            messages = existing
            self.tool_calls_used = 0  # per-invocation; never inherit history
            tail_is_complete_step = _tail_has_complete_step(existing)
            tail_step_completed = (
                classify_resume(existing) is ResumeState.STEP_COMPLETED)
            if tail_is_complete_step and invocation.retry_feedback:
                synth = repair_dangling_tool_use_on_resume(
                    messages, transcript, feedback=invocation.retry_feedback,
                    captured=_load_captured_tool_results(invocation.transcript_dir))
                if synth is not None:
                    for rb in synth:
                        elog.append_tool_result(agent_id=aid, block=rb)
            elif tail_is_complete_step:
                injected = synthesize_native_success_on_resume(
                    messages, transcript, elog, aid)
                if injected is not None:
                    resume_base += 1
                deliver_engine_continuation(
                    messages, transcript, elog, aid, RESUME_REDO_NUDGE)
            elif tail_step_completed and invocation.retry_feedback:
                deliver_engine_continuation(
                    messages, transcript, elog, aid, invocation.retry_feedback)
            elif tail_step_completed:
                deliver_engine_continuation(
                    messages, transcript, elog, aid, RESUME_REDO_NUDGE)
            elif classify_resume(existing) is ResumeState.DANGLING_TOOL_USE:
                synth = repair_dangling_tool_use_on_resume(
                    messages, transcript,
                    captured=_load_captured_tool_results(invocation.transcript_dir))
                if synth is not None:
                    for rb in synth:
                        elog.append_tool_result(agent_id=aid, block=rb)
            elif (invocation.retry_feedback
                    and classify_resume(existing) is not ResumeState.AWAITING_USER):
                deliver_engine_continuation(
                    messages, transcript, elog, aid, invocation.retry_feedback)
            resume_state = classify_resume(messages)
            if resume_state is ResumeState.AWAITING_USER:
                if ctx.user_input_fn is None:
                    self.messages = messages
                    self.ceiling = invocation.max_tool_calls
                    self.guard = LoopGuard(interactive=invocation.interactive)
                    self._pending_terminal = Error(payload={
                        "category": "config",
                        "kind": "no_user_input_fn",
                        "message": (
                            "AWAITING_USER resume requires user_input_fn; "
                            "none installed by runner"
                        ),
                    })
                    return
                reply, source = await _resolve_wait_reply(
                    invocation.coordinator, ctx.user_input_fn,
                    invocation.resume_should_continue,
                    elog=elog, agent_id=aid,
                    resume_cause=invocation.resume_cause,
                    last_agent_message=last_assistant_text(messages),
                    halt_writer=invocation.halt_writer,
                    halt_clearer=invocation.halt_clearer)
                if source == "nudge":
                    assert isinstance(reply, str)
                    deliver_engine_continuation(
                        messages, transcript, elog, aid, reply)
                else:
                    deliver_continuation_reply(
                        messages, transcript, elog, aid, reply, human=True)
                # Fall through (do NOT return): the reply drives the next LLM turn.
            drained = 0
            if resume_state is not ResumeState.AWAITING_USER:
                drained = drain_all_interjects_at_resume(
                    invocation.coordinator, messages, transcript, elog, aid)
            pending_human_tail = (
                messages[-1].role == "user"
                and not _contains_tool_result(messages[-1])
            )
            if (resume_state is not ResumeState.AWAITING_USER
                    and invocation.resume_should_continue and drained == 0
                    and not pending_human_tail):
                deliver_engine_continuation(
                    messages, transcript, elog, aid,
                    continuation_nudge(invocation.resume_cause))
            if len(messages) == resume_base:
                deliver_engine_continuation(
                    messages, transcript, elog, aid,
                    continuation_nudge(invocation.resume_cause))
        else:
            messages = []
            for m in invocation.initial_messages:
                messages.append(m)
                transcript.append(m)
            self.tool_calls_used = 0
            elog.append_step_context(
                agent_id=aid, parent_id_override=invocation.first_event_parent_id,
                provider=invocation.provider_name, model=runner.model,
                tools_schema=[{"name": _canon_tool_name(n)} for n in tools],
                params={"env.CLAUDE_CONFIG_DIR": str(config_dir)},
                max_tool_calls=invocation.max_tool_calls)
            elog.append_engine_message(
                agent_id=aid,
                message=Message(role="system",
                                content=(TextBlock(text=invocation.system_prompt),)),
                subtype="system_prompt")
            for m in invocation.initial_messages:
                if all(isinstance(b, TextBlock) for b in m.content):
                    elog.append_engine_message(
                        agent_id=aid, message=m, subtype="agent_prompt")

        self.messages = messages
        self.ceiling = invocation.max_tool_calls  # may be None (no cap)
        self.guard = LoopGuard(interactive=invocation.interactive)

        # Loop-guard turn accumulators (Fix H): one logical turn arrives as many
        # AssistantMessage fragments; accumulate and flush ONCE at ResultMessage.
        self._pending_text = ""
        self._pending_tools: dict[str, ToolOutcome] = {}
        self._pending_complete = False
        self._web_calls: dict[str, tuple[str, dict[str, Any]]] = {}

        if native_resume:
            await store.overwrite(skey, message_to_session_records(
                _project_for_cli(messages[:resume_base]),
                session_id=skey["session_id"],
                cwd=str(invocation.quest_context.project_root),
                model=runner.model,
            ))

        self._llm_group_open = False
        self._llm_group_key: str | None = None
        self._llm_blocks: list[dict[str, Any]] = []
        self._llm_tail_text: str = ""
        self._llm_model: str | None = None
        self._llm_stop_reason: Any = None
        self._llm_usage: dict[str, Any] = {}
        self._stream_usage_by_msg: dict[str, tuple[dict[str, Any], Any]] = {}
        self._stream_cur_msg_id: str | None = None
        self._stream_cur_usage: dict[str, Any] = {}
        self._stream_cur_stop_reason: Any = None
        self._flush_lock = asyncio.Lock()
        state.flush_llm_group = self._flush_llm_group

        client_ctx = self.runner._build_client(opts)
        client = await client_ctx.__aenter__()
        runner._client = client
        self.runner._client_ctx = client_ctx
        self._next_query: Any = _stream_history(messages[resume_base:])
        self._last_raw: Any = None
        from imp.engine.reincarnation import handoff_path_for
        self._handoff_phase: str | None = None
        self._handoff_path_obj = handoff_path_for(invocation.transcript_dir)

    def _flush_turn_record(self) -> TurnRecord | None:
        if (not self._pending_text and not self._pending_tools
                and not self._pending_complete):
            return None
        rec = TurnRecord(assistant_text=self._pending_text,
                         tools=list(self._pending_tools.values()),
                         has_complete_step=self._pending_complete)
        self._pending_text = ""
        self._pending_tools = {}
        self._pending_complete = False
        return rec

    async def _flush_llm_group(self, backstop_stop_reason: Any = None) -> None:
        # Serialize every flush on one per-run lock (see the old closure): the
        # eager sub_agent flush in _impl blocks here until the in-flight prose
        # emit completes, then sees the group closed and no-ops.
        async with self._flush_lock:
            if not self._llm_group_open:
                return
            client = self.runner._client
            cu_fields = await _context_usage_fields(client)
            harvested = (self._stream_usage_by_msg.pop(self._llm_group_key, None)
                         if self._llm_group_key is not None else None)
            if harvested is not None:
                usage, harvested_stop = harvested
                stop = harvested_stop if harvested_stop is not None else (
                    self._llm_stop_reason if self._llm_stop_reason is not None
                    else backstop_stop_reason)
            else:
                usage = self._llm_usage
                stop = self._llm_stop_reason if self._llm_stop_reason is not None \
                    else backstop_stop_reason
            self.elog.append_agent_message(
                agent_id=self.aid, provider=self.invocation.provider_name,
                model=self._llm_model or self.runner.model,
                raw={"content": self._llm_blocks, "stop_reason": stop,
                     "usage": usage},
                usage=usage, stop_reason=stop,
                provider_error=classifies_as_provider_error(self._llm_tail_text),
                message_id=self._llm_group_key,
                context_window=cu_fields.get("context_window"),
                context_pct=cu_fields.get("context_pct"),
                context_tokens=cu_fields.get("context_tokens"))
            self._llm_group_open = False
            self._llm_group_key = None
            self._llm_blocks = []
            self._llm_tail_text = ""
            self._llm_model = None
            self._llm_stop_reason = None
            self._llm_usage = {}

    async def _receive(self) -> Any:
        coord = self.invocation.coordinator
        client = self.runner._client
        while True:
            await client.query(self._next_query)
            requery = False
            got_result = False
            async with contextlib.aclosing(request_bracketed(
                    coord, client.receive_response())) as _stream:
                async for sdk_msg in _stream:
                    if self.runner._cancel:
                        return
                    if coord is not None:
                        coord.note_activity()
                    # Post-API-site analog (Decision 10): one sweep per SDK
                    # message; the model-API/CLI latency between messages is the
                    # longest registry-blind gap under this provider.
                    await _bg._sweep_bg(self.ctx)
                    yield sdk_msg
                    if type(sdk_msg).__name__ == "ResultMessage":
                        got_result = True
                        if self._requery_pending:
                            self._requery_pending = False
                            requery = True
                        break
            if requery:
                continue
            if got_result:
                return
            # receive_response() exhausted without a ResultMessage.
            if self.runner._cancel:
                return
            self.runner._last_no_result_diag = {
                **_subprocess_diag(client),
                "stderr_tail": self.runner._stderr_sink.tail()}
            yield _NO_RESULT_SENTINEL
            return

    async def _message_to_items(self, raw: Any) -> Any:
        if raw is _NO_RESULT_SENTINEL:
            diag = self.runner._last_no_result_diag
            message = "SDK closed without yielding a ResultMessage"
            if diag:
                message = f"{message} (exit={diag.get('exit')})"
            yield Error(payload={
                "category": "network",
                "kind": "no_result_message",
                "message": message,
                "detail": diag,
            })
            return
        elog = self.elog
        aid = self.aid
        state = self.runner._state
        client = self.runner._client
        invocation = self.invocation
        cls_name = type(raw).__name__
        if cls_name == "StreamEvent":
            ev = getattr(raw, "event", None)
            ev = ev if isinstance(ev, dict) else {}
            etype = ev.get("type")
            if etype == "message_start":
                start_msg = ev.get("message")
                start_msg = start_msg if isinstance(start_msg, dict) else {}
                self._stream_cur_msg_id = start_msg.get("id")
                u = start_msg.get("usage")
                self._stream_cur_usage = dict(u) if isinstance(u, dict) else {}
                self._stream_cur_stop_reason = None
            elif etype == "message_delta":
                u = ev.get("usage")
                if isinstance(u, dict):
                    self._stream_cur_usage.update(u)
                delta = ev.get("delta")
                if isinstance(delta, dict) and delta.get("stop_reason") is not None:
                    self._stream_cur_stop_reason = delta.get("stop_reason")
            elif etype == "message_stop":
                if self._stream_cur_msg_id is not None:
                    self._stream_usage_by_msg[self._stream_cur_msg_id] = (
                        self._stream_cur_usage, self._stream_cur_stop_reason)
                self._stream_cur_msg_id = None
                self._stream_cur_usage = {}
                self._stream_cur_stop_reason = None
        imp_msg, warns, thinkings = _sdk_message_to_imp(raw)
        for content in thinkings:
            yield Thinking(text=content)
        for w in warns:
            yield Warning(text=w)
        if imp_msg is not None:
            self.messages.append(imp_msg)
            self.transcript.append(imp_msg)

            if imp_msg.role == "assistant":
                turn_text = "".join(
                    b.text for b in imp_msg.content
                    if isinstance(b, TextBlock))
                if turn_text:
                    self._pending_text = (
                        self._pending_text + turn_text if self._pending_text
                        else turn_text)
                if cls_name == "AssistantMessage":
                    mid = getattr(raw, "message_id", None)
                    if (self._llm_group_open and mid is not None
                            and self._llm_group_key is not None
                            and mid != self._llm_group_key):
                        await self._flush_llm_group()
                    # Split at the classifying boundary so error prose gets its own node.
                    if classifies_as_provider_error(_text_of(imp_msg)) and self._llm_blocks:
                        await self._flush_llm_group()
                    frag = _serialize_claude_code_message(raw)
                    if not self._llm_group_open:
                        self._llm_group_open = True
                        self._llm_group_key = mid
                    elif self._llm_group_key is None and mid is not None:
                        self._llm_group_key = mid
                    self._llm_blocks.extend(frag["content"])
                    self._llm_tail_text = _text_of(imp_msg)
                    served = getattr(raw, "model", None)
                    if served is not None and self._llm_model is None:
                        self._llm_model = served
                    if (frag.get("stop_reason") is not None
                            and self._llm_stop_reason is None):
                        self._llm_stop_reason = frag["stop_reason"]
                    if frag.get("usage"):
                        self._llm_usage = frag["usage"]
                    # Tier A proactive handoff (Mechanism 3): on the model's
                    # context-cutoff crossing, route through the cooperative-
                    # interrupt path — signal the running query and let the SDK
                    # close this turn balanced, then continue with a plain
                    # handoff-instruction increment (handled in _is_terminal).
                    # A completing turn wins over a crossing, and probing the
                    # SDK costs a round trip, so skip the probe on one.
                    if (self._handoff_phase is None
                            and not any(
                                isinstance(b, ToolUseBlock)
                                and _canon_tool_name(b.name) == "complete_step"
                                for b in imp_msg.content)):
                        from imp.providers.catalog import (
                            sdk_context_threshold_crossed,
                        )
                        cu_fields = await _context_usage_fields(client)
                        if sdk_context_threshold_crossed(
                                cu_fields.get("context_tokens"),
                                cu_fields.get("context_window"),
                                invocation.provider_name, self.runner.model):
                            await self._flush_llm_group()
                            self.runner._handoff_requested = True
                            self._handoff_phase = "interrupt_requested"
                            with contextlib.suppress(Exception):
                                await client.interrupt()
                            return
                    if any(isinstance(b, ToolUseBlock)
                           for b in imp_msg.content):
                        await self._flush_llm_group()
                    for b in imp_msg.content:
                        if isinstance(b, ToolUseBlock):
                            if b.name in CC_WEB_BUILTIN_TO_NORM:
                                norm = CC_WEB_BUILTIN_TO_NORM[b.name]
                                self._web_calls[b.id] = (norm, b.input)
                                elog.append_tool_call(
                                    agent_id=aid,
                                    block=ToolUseBlock(id=b.id, name=norm,
                                                       input=b.input))
                                continue
                            if _canon_tool_name(b.name) == "complete_step":
                                state.complete_step_called = True
                                state.complete_step_args = b.input
                                # Part A: complete_step is terminal — re-emit the
                                # result-less tool_call, then preempt the trailing
                                # turn via interrupt.
                                if not self.runner._complete_requested:
                                    elog.append_tool_call(
                                        agent_id=aid,
                                        block=ToolUseBlock(
                                            id=b.id, name="complete_step",
                                            input=b.input))
                                    await self._flush_llm_group()
                                    self.runner._complete_requested = True
                                    with contextlib.suppress(Exception):
                                        await client.interrupt()
                                    continue
                            if (self.ceiling is not None
                                    and self.tool_calls_used >= self.ceiling):
                                ceil_msg = (
                                    f"tool_call_ceiling_exceeded (ceiling={self.ceiling})"
                                )
                                elog.append_tool_result(
                                    agent_id=aid,
                                    block=ToolResultBlock(b.id, ceil_msg,
                                                          is_error=True))
                                continue
                            self.tool_calls_used += 1
                            canon = _canon_tool_name(b.name)
                            if canon == "complete_step":
                                self._pending_complete = True
                            self._pending_tools[b.id] = ToolOutcome(
                                name=canon, tool_use_id=b.id,
                                input=b.input, is_error=True)
                            if canon == "sub_agent":
                                continue
                            elog.append_tool_call(
                                agent_id=aid,
                                block=ToolUseBlock(id=b.id, name=canon,
                                                   input=b.input))
            elif imp_msg.role == "user":
                await self._flush_llm_group()
                for b in imp_msg.content:
                    if isinstance(b, ToolResultBlock):
                        if b.tool_use_id in self._web_calls:
                            norm, call_input = self._web_calls[b.tool_use_id]
                            cites = _parse_cc_web_citations(
                                norm, call_input, b.content)
                            elog.append_tool_result(
                                agent_id=aid, block=b, citations=cites,
                                duration_ms=state.tool_durations.pop(
                                    b.tool_use_id, None))
                            continue
                        pend = self._pending_tools.get(b.tool_use_id)
                        is_task = pend is not None and pend.name == "sub_agent"
                        if not is_task:
                            elog.append_tool_result(
                                agent_id=aid, block=b,
                                duration_ms=state.tool_durations.pop(
                                    b.tool_use_id, None))
                        if pend is not None:
                            pend.is_error = b.is_error
                            pend.result_preview = \
                                _result_preview(b.content)

    def _drain_kills(self) -> list[dict[str, Any]]:
        state = self.runner._state
        infos: list[dict[str, Any]] = []
        while state.killed_tool_uses:
            infos.append(state.killed_tool_uses.pop(0))
        return infos

    def _fatal_error(self) -> dict[str, Any] | None:
        state = self.runner._state
        if state.fatal_tool_error is not None:
            tool_name, msg = state.fatal_tool_error
            return {
                "category": "config",
                "kind": "fatal_tool_error",
                "tool": tool_name,
                "message": msg,
            }
        return None

    async def _is_terminal(self, raw: Any) -> bool:
        if type(raw).__name__ != "ResultMessage":
            return False
        self._last_raw = raw
        elog = self.elog
        aid = self.aid
        ctx = self.ctx
        invocation = self.invocation
        runner = self.runner
        await self._flush_llm_group(
            backstop_stop_reason=getattr(raw, "stop_reason", None))
        elog.append_query_result(
            agent_id=aid,
            total_cost_usd=getattr(raw, "total_cost_usd", None),
            duration_ms=getattr(raw, "duration_ms", None),
            duration_api_ms=getattr(raw, "duration_api_ms", None),
            num_turns=getattr(raw, "num_turns", None),
            stop_reason=getattr(raw, "stop_reason", None),
            subtype=getattr(raw, "subtype", None),
            is_error=bool(getattr(raw, "is_error", False)),
            usage=getattr(raw, "usage", None) or {},
            model_usage=getattr(raw, "model_usage", None) or {},
            api_error_status=getattr(raw, "api_error_status", None),
            permission_denials=getattr(raw, "permission_denials", None),
            errors=getattr(raw, "errors", None))
        subtype = getattr(raw, "subtype", None)
        if (runner._complete_requested and subtype == "error_during_execution"):
            # Part A: interrupt landed — fall through to complete_step terminal;
            # deliberately do NOT balance the tail (the dangling result-less
            # complete_step stays the persisted tail).
            runner._complete_requested = False
            self._flush_turn_record()
        elif ((runner._steer_requested or runner._handoff_requested)
                and subtype == "error_during_execution"):
            if runner._handoff_requested:
                from imp.engine.reincarnation import handoff_instruction
                runner._handoff_requested = False
                self._flush_turn_record()
                repair_dangling_tool_use_on_resume(
                    self.messages, self.transcript,
                    captured=_load_captured_tool_results(invocation.transcript_dir))
                instr = handoff_instruction(self._handoff_path_obj)
                instr_msg = Message(
                    role="user", content=(TextBlock(text=instr),))
                self.messages.append(instr_msg)
                self.transcript.append(instr_msg)
                elog.append_engine_message(
                    agent_id=aid, message=instr_msg, subtype="continuation")
                self._next_query = instr
                self._handoff_phase = "instruction_sent"
                self._requery_pending = True
                return False
            # Cooperative interrupt landed: unblock the waiting interrupt() and
            # route into the attended inline-yield.
            runner._steer_unblocked.set()
            runner._steer_requested = False
            self._flush_turn_record()
            from imp.agents.tools.bg import terminate_background_processes
            from imp.agents.tools.context import terminate_running_processes
            await terminate_running_processes(ctx)
            await terminate_background_processes(ctx)
            last_text = ""
            for prev in reversed(self.messages):
                if prev.role == "assistant":
                    last_text = _text_of(prev)
                    break
            steer = build_inline_solicit(
                user_input_fn=runner._user_input_fn,
                coordinator=invocation.coordinator,
                event_log=elog, agent_id=aid,
                steer_prompt=INTERRUPT_STEER_HINT,
                interrupt=True,
                halt_writer=invocation.halt_writer,
                halt_clearer=invocation.halt_clearer)
            reply = await steer(last_text)
            if runner._interrupted():
                return False
            if invocation.coordinator is not None:
                invocation.coordinator.clear_pause()
            if isinstance(reply, str) and not reply.strip():
                deliver_engine_continuation(
                    self.messages, self.transcript, elog, aid,
                    RESUME_CONTINUE_NUDGE)
                self._next_query = RESUME_CONTINUE_NUDGE
            else:
                self._next_query = deliver_continuation_reply(
                    self.messages, self.transcript, elog, aid, reply, human=True)
            self._requery_pending = True
            return False
        elif (runner._handoff_requested or runner._steer_requested
                or runner._complete_requested):
            if runner._handoff_requested:
                runner._handoff_requested = False
                self._handoff_phase = None
            runner._steer_requested = False
            runner._complete_requested = False

        if self._handoff_phase == "instruction_sent":
            # The handoff-writing turn completed: capture the handoff and stop.
            # Wins over a stray complete_step on this turn.
            from imp.engine.reincarnation import write_handoff_note
            write_handoff_note(
                self._handoff_path_obj,
                most_recent_assistant_text(self.messages))
            self._handoff_done = True
            self._handoff_path = str(self._handoff_path_obj)
        return True

    def _terminal_error(self, last_text: str) -> dict[str, Any] | None:
        client = self.runner._client
        err = _termination_from_result(
            self._last_raw, last_text,
            {**_subprocess_diag(client),
             "stderr_tail": self.runner._stderr_sink.tail()},
            config_dir=self.runner._config_dir)
        if err is None:
            err = _classify_agent_text(last_text)
            if err is None:
                return None
            err.detail["last_agent_text"] = last_text[:500]
        _roll_back_error_turn(self.messages, self.transcript)
        err = self._settle_auth_terminality(err)
        return cast(dict[str, Any], perr.make_error_event(
            err, kind=getattr(err, "kind", ""))["payload"])

    def _settle_auth_terminality(self, err: perr.ProviderError) -> perr.ProviderError:
        """Probe the token on a server 401; upgrade to AuthTerminalError if the grant is dead."""
        if err.category != "auth":
            return err
        outcome = refresh_canonical_token(int(time.time() * 1000), force=True)
        _journal_token_refresh(self.elog, self.aid, outcome)
        if outcome.outcome != FAILED_INVALID_GRANT:
            return err
        upgraded = perr.AuthTerminalError(_DEAD_GRANT_MESSAGE)
        upgraded.detail.update(err.detail)
        return upgraded

    def _last_text(self) -> str:
        for prev in reversed(self.messages):
            if prev.role == "assistant":
                return _text_of(prev)
        return ""

    def _trailing_text(self) -> str:
        last = self.messages[-1] if self.messages else None
        if last is None or last.role != "assistant":
            return ""
        return _text_of(last)

    def _requery(self, text: Any, *, human: bool) -> None:
        self._next_query = deliver_continuation_reply(
            self.messages, self.transcript, self.elog, self.aid, text,
            human=human)

    def _drain_interject(self) -> None:
        interject_msg = _drain_interject_message(
            self.invocation.coordinator, self.elog, self.aid)
        if interject_msg is not None:
            self.messages.append(interject_msg)
            self.transcript.append(interject_msg)
            self.elog.append_user_message(
                agent_id=self.aid, message=interject_msg, subtype="interject")
            self._next_query = f"{self._next_query}\n\n{_text_of(interject_msg)}"

    def _reset_capture(self) -> None:
        state = self.runner._state
        if state is not None:
            state.complete_step_called = False
            state.complete_step_args = {}
        self.runner._complete_requested = False


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
    ) -> None:
        self.model = model_name
        self._client_factory = client_factory
        self._user_input_fn = user_input_fn
        self._last_no_result_diag: dict[str, Any] = {}
        self._cancel = False
        self._ctx: ToolContext | None = None
        self._state: _RunState | None = None
        # The live SDK client while a turn streams. interrupt() signals the
        # running query (client.interrupt()) WITHOUT disconnecting: the subprocess
        # stays connected and the turn closes balanced, so a steer or handoff
        # continues with a plain increment. None outside an active
        # `async with client_ctx`.
        self._client: Any = None
        self._client_ctx: Any = None
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
        # post-interrupt ResultMessage arrives, letting interrupt() return.
        self._steer_requested = False
        self._steer_unblocked = asyncio.Event()
        # Tier A proactive-handoff intent flag (separate from _steer_requested):
        # set when the threshold watchdog interrupts the live turn, consumed at the
        # generalized balanced-close site to continue with a handoff-instruction
        # increment.
        self._handoff_requested = False
        # Part A completion-interrupt intent flag: set when complete_step is seen
        # (preempting the trailing turn via interrupt), consumed at the balanced
        # close to fall through to agent_result.
        self._complete_requested = False

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
            # Neutralize inherited thinking/effort/output-token tuning so the
            # spawned claude's posture is imp-owned (via thinking=/effort=), not
            # host-floated. The SDK merges options.env over the inherited env, so
            # an empty string wins over any hostile value.
            "MAX_THINKING_TOKENS": "",
            "CLAUDE_CODE_DISABLE_THINKING": "",
            "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING": "",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "",
            "CLAUDE_CODE_EFFORT_LEVEL": "",
            "USER_TYPE": "",
        }
        extra_args: dict[str, Any] = {}
        if config_dir is not None:
            # imp-isolated home (fresh + replay); the batcher resolves
            # projects_dir from this env. Native resume passes config_dir=None and
            # relies on the SDK's own materialized temp dir, so no history-related
            # env/args are added here.
            env["CLAUDE_CONFIG_DIR"] = str(config_dir)
        # Standard tool mode is imp's single mode for every path (fresh and
        # resume): declare the MCP tools up front. claude-p's default ToolSearch
        # deferral would cost an extra LLM round-trip on each step's first MCP
        # call to resolve the schema, and would leave replayed tool_use blocks
        # undeclared (400 "tool use concurrency"). imp's tool-sets are small, so
        # up-front declaration is always the right trade.
        env["ENABLE_TOOL_SEARCH"] = "false"
        # Silence two claude-p injection channels imp doesn't want (the anthropic
        # provider injects neither, so off => true parity): the CLAUDE.md/memory
        # walk, and the start-of-conversation gitStatus snapshot + generic git
        # instructions (which go stale across a step's edits and fight imp's
        # controlled per-quest worktree/snapshot git model). Static, unconditional.
        env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
        env["CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"] = "1"
        # Per-call MCP wall clock: pinned to 24h so no host value caps an imp tool call.
        env["MCP_TOOL_TIMEOUT"] = "86400000"
        thinking_cfg, effort_cfg = _cc_thinking_posture(invocation.tier)
        return ClaudeAgentOptions(
            model=info.cli_model or self.model,
            betas=cast(list[SdkBeta], list(info.betas)),
            include_partial_messages=True,
            stderr=self._stderr_sink.feed if self._stderr_sink is not None else None,
            env=env,
            # Pin the `claude` subprocess to the quest's project_root. Without
            # this the SDK launches claude with cwd=None and leaves $PWD as
            # inherited, so claude keys off a stale $PWD and edits/commits the
            # wrong working tree (e.g. imp's own repo when launched from a
            # subdir or via Popen(cwd=...) which doesn't update $PWD).
            cwd=str(invocation.quest_context.project_root),
            system_prompt=invocation.system_prompt,
            thinking=thinking_cfg,
            effort=effort_cfg,
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
        session = ClaudeCodeSession(invocation, self)
        try:
            try:
                async with contextlib.aclosing(
                        run_agent_loop(invocation, session)) as loop:
                    async for ev in loop:
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
            # run()'s finally explicitly exits the client context manager
            # (graceful disconnect) then reaps the transport group as a safety
            # net, and drops the stale handle so a late interrupt() is a no-op
            # rather than disconnecting a dead client.
            if self._impty_driver is not None:
                with contextlib.suppress(Exception):
                    self._impty_driver.close()
                self._impty_driver = None
            if self._client_ctx is not None:
                with contextlib.suppress(Exception):
                    await self._client_ctx.__aexit__(None, None, None)
                self._client_ctx = None
            if self._client is not None:
                await _reap_transport_group(self._client)
            self._client = None
            if self._stderr_sink is not None:
                self._stderr_sink.close()
            if self._config_dir is not None:
                shutil.rmtree(self._config_dir, ignore_errors=True)
                self._config_dir = None

    def _interrupted(self) -> bool:
        """Read the cancel flag through a call so mypy doesn't narrow it to a
        constant after the per-message guard. interrupt() flips `_cancel` from
        another task, which mypy's flow analysis can't model — without this
        indirection the mid-turn cancel check reads as unreachable."""
        return self._cancel

    async def interrupt(self) -> None:
        # Cooperative tier ONLY: ask the SDK to end the turn balanced and wait for
        # the receive loop to set _steer_unblocked. Deliberately UNBOUNDED — every
        # caller wraps this in an outer deadline (the coordinator's 2s cooperative
        # budget; the stall watchdog's grace) and escalates to force_kill() on
        # overrun. Fire client.interrupt() BEFORE any process termination so a
        # killed-process tool_result can never race the interrupt past the stop.
        client = self._client
        if client is None or not hasattr(client, "interrupt"):
            self._cancel = True
            return
        self._steer_requested = True
        # Reset the latch on EVERY interrupt: a prior steer left it set, and a
        # stale-set Event would make the wait return immediately.
        self._steer_unblocked.clear()
        with contextlib.suppress(Exception):
            await client.interrupt()
        await self._steer_unblocked.wait()

    async def force_kill(self) -> None:
        """Non-waiting SIGKILL of the claude-p transport group on teardown overrun;
        reaping a D-state child would re-block the executor, so never await wait().
        Every force_kill caller is an interrupt/pause context, so mark the runner
        cancelled here: when the SDK subsequently raises a `-9 ProcessError` for the
        SIGKILLed child, run()'s outer except classifies it as config/interrupted
        instead of the unknown-error fallback ("unclassified provider error")."""
        self._cancel = True
        transport = getattr(self._client, "_transport", None)
        process = getattr(transport, "_process", None)
        if process is None:
            return
        with contextlib.suppress(ProcessLookupError, OSError):
            await kill_process_group(process, signal.SIGKILL)


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


def _env_check() -> bool:
    return shutil.which("claude") is not None


def _factory(model: str) -> Provider:
    return ClaudeCodeProvider(model_name=model)
