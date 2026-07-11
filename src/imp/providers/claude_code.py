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
import inspect
import json
import re
import shutil
import signal
import time
import uuid
import zoneinfo
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from imp.agents.classify import ResumeState, _contains_tool_result, classify_resume
from imp.agents.loop_guard import (
    LoopGuard,
    ToolOutcome,
    TurnRecord,
    _result_preview,
)
from imp.agents.output_guard import (
    Nudge,
    Ok,
    make_nudge_event,
    make_recovery_event,
    validate_output_or_nudge,
)
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
    change_home_path,
    kill_process_group,
    request_bracketed,
    suspend_inactivity_watchdog,
)
from imp.agents.tools.errors import FatalToolError
from imp.agents.tools.load_skill import make_load_skill_tool
from imp.agents.transcript import (
    RESUME_REDO_NUDGE,
    Transcript,
    captured_result_key,
    deliver_engine_continuation,
    deliver_reply_on_resume,
    repair_dangling_tool_use_on_resume,
    synthesize_native_success_on_resume,
)
from imp.engine.agent_run import (
    _CONTINUATION_NUDGE,
    INTERRUPT_STEER_HINT,
    _resolve_wait_reply,
    build_inline_solicit,
    drain_all_interjects_at_resume,
    last_assistant_text,
)
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.engine.drivers import driver_allowed_tools
from imp.providers import errors as perr
from imp.providers._cc_builtins import ALL_CLAUDE_CODE_BUILTIN_TOOLS
from imp.providers.base import Provider
from imp.providers.claude_code_config_dir import materialize_fresh_config_dir
from imp.providers.claude_code_session_store import (
    _SESSION_LIMIT_RE,
    reconcile_store_tail,
    resume_session_id,
    session_key,
    session_store_for,
)
from imp.providers.claude_token_refresh import refresh_canonical_token
from imp.providers.resolve import spawn_subagent_via_registry
from imp.providers.web_native import (
    CC_WEB_BUILTIN_TO_NORM,
    WEB_TOOL_NAMES,
    cc_enabled_web_builtins,
    check_web_capability,
)

if TYPE_CHECKING:
    from claude_agent_sdk import EffortLevel, ThinkingConfig

_AUTH_PATTERNS = (
    "Invalid API key",
    "Please run `/login`",
    "Please run /login",
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

# Matches the CLI session-limit reset announcement, e.g.
# "resets 4am (America/Los_Angeles)", "resets 4:30pm (...)", "resets 16:00 (...)".
_RESET_RE = re.compile(
    r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*\(([^)]+)\)",
    re.IGNORECASE,
)
_RESET_MARGIN_SECONDS = 120.0
_MAX_RESET_WAIT_SECONDS = 6 * 3600.0


def _reset_hint_seconds(text: str, now: float) -> float | None:
    """Seconds to wait until the announced session-limit reset, + a 2-minute
    margin, or None. `now` is epoch seconds, injected for testability.

    Returns None on any parse failure, an unknown/bad zone, an out-of-range
    time, or a computed wait over 6 h (session windows are 5 h, so a larger
    figure means a bad parse we distrust). None means exactly today's behavior.
    """
    m = _RESET_RE.search(text)
    if m is None:
        return None
    hour_s, minute_s, ampm, zone_name = m.groups()
    try:
        tz = zoneinfo.ZoneInfo(zone_name)
    except (KeyError, ValueError):
        # ZoneInfoNotFoundError subclasses KeyError; a malformed name -> ValueError.
        return None
    hour = int(hour_s)
    minute = int(minute_s) if minute_s else 0
    if ampm:
        if not 1 <= hour <= 12:
            return None
        if ampm.lower() == "pm" and hour != 12:
            hour += 12
        elif ampm.lower() == "am" and hour == 12:
            hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    now_local = datetime.fromtimestamp(now, tz=tz)
    target = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_local:
        target += timedelta(days=1)
    delta = target.timestamp() - now
    if delta > _MAX_RESET_WAIT_SECONDS:
        return None
    return delta + _RESET_MARGIN_SECONDS


def _rate_limit_with_reset_hint(text: str) -> perr.RateLimitError:
    """Build the session-limit RateLimitError, attaching the parsed reset time as
    detail={"retry_after_seconds": <hint>} when one is available. The key is
    disjoint from every diagnostic key _termination_from_result later merges via
    labeled.detail.update(...), so a hint set here survives that merge."""
    hint = _reset_hint_seconds(text, time.time())
    detail: dict[str, object] | None = (
        {"retry_after_seconds": hint} if hint is not None else None
    )
    return perr.RateLimitError(f"claude CLI: {text[:200]}", detail=detail)


def _classify_agent_text(
    text: str, *, anchored: bool = True,
) -> perr.ProviderError | None:
    """Detect known claude CLI terminal-error patterns in agent text.

    The CLI catches its own errors and returns them as a text response
    with exit 0 (no out-of-band signal). Exact-substring patterns,
    conservatively chosen: false negatives fall back to existing
    retry behavior; false positives would convert recoverable agent
    confusion into fatal pause.

    When `anchored` (the default), patterns must match within the first line
    of the text: every known genuine CLI error is a terse message that leads
    with the error, while an agent that merely *quotes* a pattern buries it
    mid-document behind a prose opener (the redu99 false positive — a
    subagent report about _AUTH_PATTERNS was reclassified as AuthError, its
    successful turn rolled back, and the retry returned an empty stub).
    Callers that already hold an out-of-band error signal
    (_termination_from_result) pass anchored=False: there the substring only
    picks the error category, and a miss degrades to TerminatedError, not a
    false pause.

    Auth/quota patterns are checked first so they win when both substrings
    appear. The API-error regex matches "API Error: <code>":
    401/403 → AuthError with kind="http_<code>"; 408 → NetworkError (transient
    timeout); 429 → RateLimitError; other 4xx → ApiError with kind="http_<code>";
    5xx → NetworkError (network category → RetryWithBackoff; §10).
    """
    scan = text.lstrip().split("\n", 1)[0] if anchored else text
    for pat in _AUTH_PATTERNS:
        if pat in scan:
            return perr.AuthError(f"claude CLI: {pat}")
    for pat in _QUOTA_PATTERNS:
        if pat in scan:
            return perr.QuotaError(f"claude CLI: {pat}")
    if _SESSION_LIMIT_RE.search(scan):
        return _rate_limit_with_reset_hint(text)
    m = _API_ERROR_RE.search(scan)
    if m:
        code = int(m.group(1))
        if code == 429:
            return _rate_limit_with_reset_hint(text)
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
    elif "API Error:" in scan:
        # A statusless `API Error:` — no routable HTTP code — means the request
        # never reached the server (ConnectionRefused, reset, EOF, transport
        # timeout). Broad transport stance: transient -> retry, not pause. A
        # 3xx/unrouted code that matched the regex above is NOT statusless, so
        # it falls through to None (Pause) here.
        return perr.NetworkError(f"claude CLI: {text[:200]}")
    return None


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


def _pre_spawn_token_refresh(elog: Any, aid: str) -> None:
    """Best-effort refresh of the canonical OAuth access token just before a
    spawn is built, journaling one audit event with the outcome. Never raises:
    refresh_canonical_token maps every failure to an outcome. The payload carries
    only the outcome string (and expiresAt when refreshed) — never token
    material, which RefreshOutcome structurally cannot hold."""
    outcome = refresh_canonical_token(int(time.time() * 1000))
    payload: dict[str, Any] = {"outcome": outcome.outcome}
    if outcome.expires_at is not None:
        payload["expires_at"] = outcome.expires_at
    elog.append(agent_id=aid, type="token_refresh", payload=payload)


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
    if isinstance(labeled, perr.AuthError):
        detail.update(_read_token_expiry(config_dir, int(time.time() * 1000)))
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
    # Per-tool wall-time (ms) keyed by SDK tool_use_id, written by the MCP
    # wrapper and read once by the receive loop when it appends the tool_result.
    tool_durations: dict[str, float] = field(default_factory=dict)
    # The flush-lock-guarded _flush_llm_group closure, assigned once by _run so
    # the out-of-band _impl sub_agent path can commit accumulated parent prose
    # (as an llm_response) BEFORE it eagerly appends the sub_agent tool_call.
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
                    # Commit any accumulated parent prose as an llm_response BEFORE
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
                # after the next yielded message and emits agent_output.
                state.complete_step_called = True
                state.complete_step_args = args
                return _tool_response("step complete", is_error=False)
            if name == "load_skill" and state.flush_llm_group is not None:
                # Commit any accumulated turn prose as an llm_response BEFORE
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
        # close to fall through to agent_output.
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
        # controlled per-change worktree/snapshot git model). Static, unconditional.
        env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
        env["CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"] = "1"
        thinking_cfg, effort_cfg = _cc_thinking_posture(invocation.tier)
        return ClaudeAgentOptions(
            model=info.cli_model or self.model,
            betas=cast(list[SdkBeta], list(info.betas)),
            include_partial_messages=True,
            stderr=self._stderr_sink.feed if self._stderr_sink is not None else None,
            env=env,
            # Pin the `claude` subprocess to the change's project_root. Without
            # this the SDK launches claude with cwd=None and leaves $PWD as
            # inherited, so claude keys off a stale $PWD and edits/commits the
            # wrong working tree (e.g. imp's own repo when launched from a
            # subdir or via Popen(cwd=...) which doesn't update $PWD).
            cwd=str(invocation.change_context.project_root),
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
        from imp.providers.stderr_sink import build_stderr_sink
        self._stderr_sink = build_stderr_sink(
            invocation.transcript_dir / "stderr.log",
            elog=elog, aid=aid, source="claude")

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
        ctx.test_gates = invocation.test_gates
        ctx.non_interactive = invocation.headless
        ctx.sandbox_policy = invocation.sandbox_policy
        ctx.checkpoint_threshold = invocation.checkpoint_threshold
        ctx.event_log = elog
        if self._user_input_fn is not None:
            ctx.user_input_fn = self._user_input_fn
        self._ctx = ctx

        state = _RunState()
        state.agent_id = aid
        state.runner_loop = asyncio.get_running_loop()
        self._state = state
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
        # absent a stored session it reconstructs the store from the transcript
        # and still resumes natively (the no-store branch).
        # resume= and session_id are mutually exclusive in the SDK: native resume
        # passes resume= and leaves session_id unset.
        existing = transcript.load(
            ctx.change_home / "blobs" if ctx.change_home is not None else None)
        is_fresh = not existing
        # A subagent's done-signal is a bare-turn-end terminal (assistant prose,
        # no complete_step), complementing _tail_has_complete_step's top-level
        # complete_step tail: recognize it here and re-emit the final text as the
        # deliverable rather than native-resuming (which hands a finished
        # subagent back to claude-p) or soliciting the human (AWAITING_USER).
        if (invocation.subagent_depth > 0 and not is_fresh
                and classify_resume(existing) is ResumeState.AWAITING_USER):
            final_text = last_assistant_text(existing)
            if final_text:
                yield {"type": "agent_output", "payload": {"text": final_text}}
            return
        store = session_store_for(invocation.transcript_dir)
        skey = session_key(invocation.change_context.project_root, aid)
        # Native resume is sound ONLY when existing's final turn was actually
        # flushed to the store. The SDK mirror batcher flushes solely at a `result`
        # boundary, so AWAITING_USER (the agent ended its turn on assistant prose —
        # a bare turn-end yield) is the only shape whose tail is already flushed.
        # SEND_TO_LLM ends on an unflushed user turn but is handled on the native
        # path via reconcile-and-overwrite: the unflushed tail is written into the
        # store before claude-p loads its native session. A genuinely DANGLING
        # tool_use likewise resumes natively (with a store) — claude-p's own pairing
        # injects the is_error placeholder, which is correct for an unfinished call.
        # A trailing complete_step is balanced with a SUCCESS result inside the
        # loaded session and the handback rides the streamed delta, and a no-store
        # resume reconstructs the store from the transcript.
        store_body = await store.load(skey)
        # Every well-formed resume takes the native path. A trailing complete_step
        # is balanced with a SUCCESS tool_result folded INTO the loaded session
        # (synthesize_native_success_on_resume), so claude-p never auto-pairs an
        # is_error; the handback (retry_feedback) rides the streamed delta. The
        # store is reconstructed from the transcript when absent.
        # Shape eligibility for native resume — decided WITHOUT the store. A
        # dispatch-only tail collapses resume_base to 0 (fresh_stream) and rebuilds
        # the store from scratch.
        shape_native_eligible = (
            (not is_fresh)
            and classify_resume(existing) in (
                ResumeState.AWAITING_USER, ResumeState.SEND_TO_LLM,
                ResumeState.DANGLING_TOOL_USE, ResumeState.STEP_COMPLETED)
        )
        # Native resume sends only messages[resume_base:] — the turns appended on
        # THIS entry; claude-p already holds the rest in its loaded session.
        resume_base = len(existing)
        # A SEND_TO_LLM tail needs the LLM to RESPOND to a trailing user turn,
        # but claude-p never generates from a loaded --resume session alone — it
        # blocks reading stdin for an input message. Folding that user turn into
        # the native session (resume_base = len) makes the streamed delta empty,
        # so claude-p waits forever and the runner parks in receive_response()
        # (a deadlock broken only by the inactivity watchdog). Instead, keep the
        # trailing unanswered user turn(s) OUT of the reconciled session and
        # stream them as the delta — they are the message that kicks the turn.
        if (shape_native_eligible
                and classify_resume(existing) is ResumeState.SEND_TO_LLM):
            while (resume_base > 0
                   and existing[resume_base - 1].role == "user"
                   and not _contains_tool_result(existing[resume_base - 1])):
                resume_base -= 1
        # Dispatch-only respawn: resume_base collapsed to 0, so the whole (short)
        # history streams fresh and the store is truncated/rebuilt from scratch.
        fresh_stream = shape_native_eligible and resume_base == 0
        # native_resume is False only on the fresh path; it still routes config_dir
        # (elif native_resume) and the reconcile_store_tail block (if native_resume).
        native_resume = shape_native_eligible
        # Best-effort: keep canonical OAuth token fresh before either disposition copies it.
        _pre_spawn_token_refresh(elog, aid)
        config_dir: Path | None = None
        if is_fresh or fresh_stream:
            config_dir = materialize_fresh_config_dir()
            self._config_dir = config_dir
            opt_store, opt_session_id, opt_resume = (
                store, resume_session_id(aid), None)
        elif native_resume:
            # Native resume: hand claude-p the loaded session by id; config_dir
            # stays None (the only disposition with no ephemeral home).
            opt_store, opt_session_id, opt_resume = (
                store, None, resume_session_id(aid))
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
            # Mid-tool Ctrl+C (spec §6.15) can leave a dangling tool_use.
            # Synthesize a placeholder tool_result so the tail is balanced and
            # emit `tool_result` events so `imp log` shows what the agent sees on
            # the next turn. This applies to native resume too: claude-p does NOT
            # auto-pair-and-generate a dangling tail from its loaded session — it
            # parks reading stdin, so the runner deadlocks in receive_response().
            # The synth result is appended AFTER resume_base was captured, so it
            # streams as the delta (claude-p pairs the dangling call in the loaded
            # session and the result kicks the next turn).
            # Native tail-balancing.
            #  - Rework (retry_feedback) + complete_step tail: leave the
            #    complete_step DANGLING in the loaded session and stream an
            #    is_error result whose content IS the feedback as the delta —
            #    claude-p pairs the dangling call against it, so the feedback
            #    reaches the model AS the result of its own complete_step call
            #    (parity with the anthropic/openai is_error path). A SUCCESS
            #    fold here would pre-balance the call and the feedback would
            #    never reach the model.
            #  - Bare backward goto (no feedback) + complete_step tail: fold a
            #    SUCCESS result INTO the loaded session (resume_base advances
            #    over it) so claude-p never auto-pairs an is_error telling the
            #    agent its accepted work failed, then kick the otherwise-empty
            #    delta with a redo nudge.
            #  - Any other dangling tool_use keeps the placeholder repair.
            #    Such a genuinely-dangling non-complete_step tail is a
            #    mid-tool interrupt that resumes as a forward-continue with
            #    retry_feedback=None, so this arm never coincides with feedback
            #    (the if/elif ordering would otherwise drop it — it cannot here).
            #  - A mid-step forward tail (SEND_TO_LLM) carrying rework feedback
            #    keeps the UNCHANGED separate-user-turn handback — the spec's
            #    removal scope is completed (complete_step-tail) steps only, and
            #    such a tail can still carry retry_feedback. The AWAITING_USER
            #    exclusion is preserved here (that shape delivers its reply via
            #    the AWAITING_USER arm below, not this handback).
            # Capture the tail shape BEFORE synthesis: synthesize_*/repair_*
            # append a user turn to `messages` (which IS `existing`), so a
            # later _tail_has_complete_step(existing) would wrongly return False.
            tail_is_complete_step = _tail_has_complete_step(existing)
            # STEP_COMPLETED: trailing prose over an ALREADY-resolved
            # complete_step. Unlike the dangling arms above, the result is
            # already in the loaded session, so there is NO SUCCESS-fold and
            # NO resume_base advance — rewriting the accepted result to
            # is_error would lie about history. Deliver the handback (feedback
            # or redo nudge) as a plain trailing user turn; it becomes the
            # streamed kicking delta and re-drives the step (an empty delta
            # would deadlock receive_response()).
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
                deliver_reply_on_resume(
                    messages, transcript, elog, aid, RESUME_REDO_NUDGE, human=False)
            elif tail_step_completed and invocation.retry_feedback:
                deliver_reply_on_resume(
                    messages, transcript, elog, aid, invocation.retry_feedback, human=False)
            elif tail_step_completed:
                deliver_reply_on_resume(
                    messages, transcript, elog, aid, RESUME_REDO_NUDGE, human=False)
            elif classify_resume(existing) is ResumeState.DANGLING_TOOL_USE:
                synth = repair_dangling_tool_use_on_resume(
                    messages, transcript,
                    captured=_load_captured_tool_results(invocation.transcript_dir))
                if synth is not None:
                    for rb in synth:
                        elog.append_tool_result(agent_id=aid, block=rb)
            elif (invocation.retry_feedback
                    and classify_resume(existing) is not ResumeState.AWAITING_USER):
                deliver_reply_on_resume(
                    messages, transcript, elog, aid, invocation.retry_feedback, human=False)
            resume_state = classify_resume(messages)
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
                # _resolve_wait_reply owns the SOLE awaiting_user_input emit and
                # fires it only when it actually solicits. Route by source: a
                # human turn ('solicited'/'interject') renders as `>`; a 'nudge'
                # is an internal engine continuation, not a user turn.
                reply, source = await _resolve_wait_reply(
                    invocation.coordinator, ctx.user_input_fn,
                    invocation.resume_should_continue,
                    elog=elog, agent_id=aid)
                if source == "nudge":
                    deliver_engine_continuation(
                        messages, transcript, elog, aid, reply)
                else:
                    deliver_reply_on_resume(
                        messages, transcript, elog, aid, reply, human=True)
                # Fall through (do NOT return): the reply drives the next LLM
                # turn as the transcript's trailing user message.
            # §2 resume-entry drain: fold pending interjects into the history
            # that feeds _stream_history(messages) on the first query. The SDK
            # is queried with the WHOLE transcript as a stream, so appending
            # user Messages here is sufficient — no separate string fold needed.
            # Guarded for non-AWAITING_USER: the AWAITING_USER join is handled above.
            drained = 0
            if resume_state is not ResumeState.AWAITING_USER:
                drained = drain_all_interjects_at_resume(
                    invocation.coordinator, messages, transcript, elog, aid)
            # A mid-tool Ctrl-C resumes as SEND_TO_LLM/DANGLING (the dangling call
            # was repaired with a synthetic result), never AWAITING_USER, so the
            # _resolve_wait_reply nudge above can't reach it. Append the same
            # continuation nudge as the trailing user turn (after any manifest) so
            # a no-message resume continues the step instead of bare-turn-ending on
            # a clarifying question. A drained human interject already drives
            # continuation, so it wins (parity with AWAITING_USER).
            pending_human_tail = (
                messages[-1].role == "user"
                and not _contains_tool_result(messages[-1])
            )
            if (resume_state is not ResumeState.AWAITING_USER
                    and invocation.resume_should_continue and drained == 0
                    and not pending_human_tail):
                deliver_engine_continuation(
                    messages, transcript, elog, aid,
                    _CONTINUATION_NUDGE)
            # Native folded-tool_result tail: after the fold, a transcript ending
            # exactly on the tool_result leaves messages[resume_base:] empty.
            # claude-p never generates from a loaded --resume session with an empty
            # delta (it parks reading stdin and the runner deadlocks in
            # receive_response()). Use the continuation nudge, not the redo nudge:
            # the folded work stands, so the resumed turn continues from it rather
            # than redoing it.
            if len(messages) == resume_base:
                deliver_engine_continuation(
                    messages, transcript, elog, aid, _CONTINUATION_NUDGE)
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
                    elog.append_user_message(agent_id=aid, message=m, human=False)

        ceiling = invocation.max_tool_calls  # may be None (no cap)

        # Loop guard (Fix H). claude_code streams ONE logical turn as multiple
        # AssistantMessage objects (thinking, then text, then tool_use) — see
        # _merge_assistant_fragments. Accumulate the whole turn into pending_*
        # and flush EXACTLY ONCE at the ResultMessage boundary (true end-of-turn).
        guard = LoopGuard(interactive=invocation.interactive)
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

        if native_resume:
            reconciled = reconcile_store_tail(
                messages[:resume_base], store_body or [],
                session_id=skey["session_id"],
                cwd=str(invocation.change_context.project_root),
                model=self.model,
            )
            await store.overwrite(skey, reconciled)

        llm_group_open = False
        llm_group_key: str | None = None
        llm_blocks: list[dict[str, Any]] = []
        llm_model: str | None = None
        llm_stop_reason: Any = None
        llm_usage: dict[str, Any] = {}
        # Finalized per-turn figures harvested from the raw Anthropic stream
        # events (StreamEvent). Keyed by streaming message id; each entry is
        # (usage, stop_reason). Popped on flush so it never grows across a run.
        stream_usage_by_msg: dict[str, tuple[dict[str, Any], Any]] = {}
        stream_cur_msg_id: str | None = None
        stream_cur_usage: dict[str, Any] = {}
        stream_cur_stop_reason: Any = None

        _flush_lock = asyncio.Lock()

        async def _flush_llm_group(backstop_stop_reason: Any = None) -> None:
            nonlocal llm_group_open, llm_group_key, llm_blocks
            nonlocal llm_model, llm_stop_reason, llm_usage
            # Serialize every flush on one per-run lock: whichever caller flushes
            # first holds the lock across the _context_usage_fields await and the
            # emit; a concurrent second caller (the eager sub_agent flush in
            # _impl) BLOCKS on the lock until that emit completes, then sees the
            # group closed and no-ops. A bare no-op guard would NOT reorder — under
            # the pinned interleaving the receive-loop flush is suspended in the
            # await when _impl fires, so the prose would still emit AFTER _impl's
            # tool_call. The lock is the completion barrier that makes _impl's
            # flush wait for the in-flight prose emit.
            async with _flush_lock:
                if not llm_group_open:
                    return
                cu_fields = await _context_usage_fields(client)
                # Prefer the finalized figures harvested from the stream for this
                # message_id; fall back to the AssistantMessage-derived values when
                # the turn produced no stream events for that key.
                harvested = (stream_usage_by_msg.pop(llm_group_key, None)
                             if llm_group_key is not None else None)
                if harvested is not None:
                    usage, harvested_stop = harvested
                    stop = harvested_stop if harvested_stop is not None else (
                        llm_stop_reason if llm_stop_reason is not None
                        else backstop_stop_reason)
                else:
                    usage = llm_usage
                    stop = llm_stop_reason if llm_stop_reason is not None \
                        else backstop_stop_reason
                elog.append_llm_response(
                    agent_id=aid, provider=invocation.provider_name,
                    model=llm_model or self.model,
                    raw={"content": llm_blocks, "stop_reason": stop,
                         "usage": usage},
                    usage=usage, stop_reason=stop,
                    message_id=llm_group_key,
                    context_window=cu_fields.get("context_window"),
                    context_pct=cu_fields.get("context_pct"),
                    context_tokens=cu_fields.get("context_tokens"))
                llm_group_open = False
                llm_group_key = None
                llm_blocks = []
                llm_model = None
                llm_stop_reason = None
                llm_usage = {}

        state.flush_llm_group = _flush_llm_group

        client_ctx = self._build_client(opts)
        async with client_ctx as client:
            # Publish the live client so the cooperative interrupt() (called from
            # the watchdog/pause task) can signal the running query and let the turn
            # close balanced; only the hard-interrupt escalation disconnects, to
            # break a parked receive_response().
            self._client = client
            # FIRST query streams the prepared history; re-queries send a plain
            # next_text string (asymmetry preserved from the pre-restructure
            # code). Each outer iteration runs its OWN receive_response() so a
            # follow-up turn is consumed from a fresh generator (issue 7:
            # receive_response() terminates after the first ResultMessage).
            next_query: Any = _stream_history(messages[resume_base:])
            from imp.engine.reincarnation import handoff_instruction, handoff_path_for
            from imp.providers.catalog import sdk_context_threshold_crossed
            # Tier A proactive-handoff watch state (Mechanism 3).
            handoff_phase: str | None = None
            handoff_path = handoff_path_for(invocation.transcript_dir)
            while True:
                await client.query(next_query)
                requery = False

                async for sdk_msg in request_bracketed(
                        invocation.coordinator, client.receive_response()):
                    if self._cancel:
                        yield {"type": "error", "payload": {
                            "category": "config",
                            "kind": "interrupted",
                            "message": "runner cancelled by interrupt",
                        }}
                        return

                    if invocation.coordinator is not None:
                        invocation.coordinator.note_activity()

                    # Post-API-site analog (Decision 10): one sweep per
                    # received SDK message — the model-API/CLI latency between
                    # messages is the longest registry-blind gap under this
                    # provider. Cancel check stays first (interrupt priority).
                    await _bg._sweep_bg(ctx)

                    cls_name = type(sdk_msg).__name__
                    if cls_name == "StreamEvent":
                        # message_start carries accurate input/cache tokens (and
                        # a priming output_tokens) plus the streaming message id;
                        # message_delta overlays the finalized cumulative
                        # output_tokens and the real stop_reason; message_stop
                        # finalizes the message into stream_usage_by_msg. The
                        # message id is tracked from message_start because the
                        # delta/stop events do not repeat it.
                        ev = getattr(sdk_msg, "event", None)
                        ev = ev if isinstance(ev, dict) else {}
                        etype = ev.get("type")
                        if etype == "message_start":
                            start_msg = ev.get("message")
                            start_msg = start_msg if isinstance(start_msg, dict) else {}
                            stream_cur_msg_id = start_msg.get("id")
                            u = start_msg.get("usage")
                            stream_cur_usage = dict(u) if isinstance(u, dict) else {}
                            stream_cur_stop_reason = None
                        elif etype == "message_delta":
                            u = ev.get("usage")
                            if isinstance(u, dict):
                                stream_cur_usage.update(u)
                            delta = ev.get("delta")
                            if isinstance(delta, dict) and delta.get("stop_reason") is not None:
                                stream_cur_stop_reason = delta.get("stop_reason")
                        elif etype == "message_stop":
                            if stream_cur_msg_id is not None:
                                stream_usage_by_msg[stream_cur_msg_id] = (
                                    stream_cur_usage, stream_cur_stop_reason)
                            stream_cur_msg_id = None
                            stream_cur_usage = {}
                            stream_cur_stop_reason = None
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
                                mid = getattr(sdk_msg, "message_id", None)
                                if (llm_group_open and mid is not None
                                        and llm_group_key is not None
                                        and mid != llm_group_key):
                                    await _flush_llm_group()
                                frag = _serialize_claude_code_message(sdk_msg)
                                if not llm_group_open:
                                    llm_group_open = True
                                    llm_group_key = mid
                                elif llm_group_key is None and mid is not None:
                                    llm_group_key = mid
                                llm_blocks.extend(frag["content"])
                                served = getattr(sdk_msg, "model", None)
                                if served is not None and llm_model is None:
                                    llm_model = served
                                if (frag.get("stop_reason") is not None
                                        and llm_stop_reason is None):
                                    llm_stop_reason = frag["stop_reason"]
                                if frag.get("usage"):
                                    llm_usage = frag["usage"]
                                # Tier A proactive handoff (Mechanism 3): on the
                                # configured context-threshold crossing, route
                                # through the cooperative-interrupt path — signal the
                                # running query and let the SDK close this turn
                                # balanced, then continue the live session with a
                                # plain handoff-instruction increment (handled at the
                                # generalized balanced-close site below). Do NOT
                                # re-stream history here: interrupt() does not
                                # disconnect the subprocess, so a replay would append
                                # on top of the already-near-full context and double
                                # it. complete_step_called is set later (per-block
                                # loop), so detect the completing turn from blocks
                                # here, canonicalizing the name: the SDK presents it
                                # as mcp__imp__complete_step, so a bare compare would
                                # miss it and wrongly preempt it.
                                if (handoff_phase is None
                                        and invocation.checkpoint_threshold is not None):
                                    completing = any(
                                        isinstance(b, ToolUseBlock)
                                        and _canon_tool_name(b.name) == "complete_step"
                                        for b in imp_msg.content)
                                    cu_fields = await _context_usage_fields(client)
                                    if (not completing
                                            and sdk_context_threshold_crossed(
                                                cu_fields.get("context_tokens"),
                                                cu_fields.get("context_window"),
                                                invocation.checkpoint_threshold)):
                                        # Flush the crossing turn's LLM group, signal
                                        # the interrupt, and mark the phase so the
                                        # watchdog cannot re-fire while the interrupt
                                        # settles. `continue` (not break) skips this
                                        # crossing message's remaining blocks but keeps
                                        # the receive loop iterating, so the balanced
                                        # error_during_execution ResultMessage reaches
                                        # the generalized close handler below — exactly
                                        # as the steer path's external interrupt does.
                                        await _flush_llm_group()
                                        self._handoff_requested = True
                                        handoff_phase = "interrupt_requested"
                                        with contextlib.suppress(Exception):
                                            await client.interrupt()
                                        continue
                            # Close the coalesced prose group at the text->tool_use
                            # transition: emit its llm_response BEFORE this fragment's
                            # tool_calls, so narration renders above the tools it
                            # introduced. Guard on tool-bearing fragments only — a
                            # pure-text fragment must not flush (its text is still
                            # accumulating toward one llm_response). The flush is a
                            # no-op when the group is already closed.
                            if any(isinstance(b, ToolUseBlock)
                                   for b in imp_msg.content):
                                await _flush_llm_group()
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
                                    # Dual-write: test fakes don't drive the MCP wrapper; production
                                    # wrapper also sets these — idempotent. SDK presents the tool as
                                    # mcp__imp__complete_step so canonicalize before comparing.
                                    if _canon_tool_name(b.name) == "complete_step":
                                        state.complete_step_called = True
                                        state.complete_step_args = b.input
                                        # Part A: complete_step is terminal — re-emit
                                        # the result-less tool_call (parity with
                                        # anthropic/openai), then preempt the trailing
                                        # turn via interrupt (args already captured from
                                        # b.input; `continue` skips the normal append
                                        # path, re-emitted here).
                                        if not self._complete_requested:
                                            elog.append_tool_call(
                                                agent_id=aid,
                                                block=ToolUseBlock(
                                                    id=b.id, name="complete_step",
                                                    input=b.input))
                                            await _flush_llm_group()
                                            self._complete_requested = True
                                            with contextlib.suppress(Exception):
                                                await client.interrupt()
                                            continue
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
                            await _flush_llm_group()
                            # ToolResultBlocks generated by claude calling our MCP
                            # tools. Append the corresponding tool_result events.
                            for b in imp_msg.content:
                                if isinstance(b, ToolResultBlock):
                                    if b.tool_use_id in web_calls:
                                        norm, call_input = web_calls[b.tool_use_id]
                                        cites = _parse_cc_web_citations(
                                            norm, call_input, b.content)
                                        elog.append_tool_result(
                                            agent_id=aid, block=b, citations=cites,
                                            duration_ms=state.tool_durations.pop(
                                                b.tool_use_id, None))
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
                                        elog.append_tool_result(
                                            agent_id=aid, block=b,
                                            duration_ms=state.tool_durations.pop(
                                                b.tool_use_id, None))
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
                        await _flush_llm_group(
                            backstop_stop_reason=getattr(
                                sdk_msg, "stop_reason", None))
                        elog.append_query_result(
                            agent_id=aid,
                            total_cost_usd=getattr(sdk_msg, "total_cost_usd", None),
                            duration_ms=getattr(sdk_msg, "duration_ms", None),
                            duration_api_ms=getattr(sdk_msg, "duration_api_ms", None),
                            num_turns=getattr(sdk_msg, "num_turns", None),
                            stop_reason=getattr(sdk_msg, "stop_reason", None),
                            subtype=getattr(sdk_msg, "subtype", None),
                            is_error=bool(getattr(sdk_msg, "is_error", False)),
                            usage=getattr(sdk_msg, "usage", None) or {},
                            model_usage=getattr(sdk_msg, "model_usage", None) or {},
                            api_error_status=getattr(sdk_msg, "api_error_status", None),
                            permission_denials=getattr(sdk_msg, "permission_denials", None),
                            errors=getattr(sdk_msg, "errors", None))
                        if (self._complete_requested
                                and getattr(sdk_msg, "subtype", None)
                                == "error_during_execution"):
                            # Part A: interrupt landed — fall through to agent_output;
                            # deliberately do NOT balance the tail so the dangling
                            # result-less complete_step stays the persisted tail (parity
                            # with anthropic/openai; the Part B resume relies on it).
                            self._complete_requested = False
                            _flush_pending()
                        elif ((self._steer_requested or self._handoff_requested)
                                and getattr(sdk_msg, "subtype", None)
                                == "error_during_execution"):
                            if self._handoff_requested:
                                # Tier A proactive handoff: the SDK closed the
                                # crossing turn balanced. Continue the LIVE (now
                                # balanced) session with a plain handoff-instruction
                                # increment — NOT a _stream_history replay, which
                                # would double the already-near-full context.
                                self._handoff_requested = False
                                # Discard the interrupted turn's partial accumulators
                                # so they don't bleed into the handoff requery's loop
                                # guard (mirrors the steer cleanup below).
                                _flush_pending()
                                # Balance the crossing turn's dangling tool_use in
                                # imp's in-memory messages/transcript (used by the
                                # later transcript stash); it no longer feeds a live
                                # replay.
                                repair_dangling_tool_use_on_resume(
                                    messages, transcript,
                                    captured=_load_captured_tool_results(invocation.transcript_dir))
                                instr = handoff_instruction(handoff_path)
                                instr_msg = Message(
                                    role="user",
                                    content=(TextBlock(text=instr),))
                                messages.append(instr_msg)
                                transcript.append(instr_msg)
                                elog.append_user_message(
                                    agent_id=aid, message=instr_msg, human=False)
                                next_query = instr
                                handoff_phase = "instruction_sent"
                                requery = True
                                break
                            # Cooperative interrupt landed: the SDK closed this
                            # turn balanced. Unblock the waiting interrupt() and
                            # route into the attended inline-yield instead of
                            # treating the error result as a termination.
                            self._steer_unblocked.set()
                            self._steer_requested = False
                            # Discard the interrupted turn's partial accumulators
                            # so they don't bleed into the next turn's loop-guard.
                            _flush_pending()
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
                                event_log=elog, agent_id=aid,
                                steer_prompt=INTERRUPT_STEER_HINT,
                                interrupt=True)
                            # Raises AwaitingUserInput when no human is attached;
                            # the balanced session makes a resume continue with no 400.
                            reply = await steer(last_text)
                            if self._interrupted():
                                return
                            # Brake holds until reply is in hand: clear only now.
                            if invocation.coordinator is not None:
                                invocation.coordinator.clear_pause()
                            user_msg = Message(
                                role="user",
                                content=(TextBlock(text=reply),))
                            messages.append(user_msg)
                            transcript.append(user_msg)
                            elog.append_user_message(
                                agent_id=aid, message=user_msg, human=True)
                            next_query = reply
                            requery = True
                            break
                        elif (self._handoff_requested or self._steer_requested
                                or self._complete_requested):
                            # Non-error_during_execution close while a flag was set:
                            # the cooperative interrupt() did not land. Reset both flags
                            # so neither bleeds into a later unrelated
                            # error_during_execution result.
                            if self._handoff_requested:
                                self._handoff_requested = False
                                handoff_phase = None
                            self._steer_requested = False
                            self._complete_requested = False

                        # Loop guard: ResultMessage is the true end-of-turn
                        # marker, so flush the accumulated turn here EXACTLY ONCE
                        # (a turn ending in complete_step set pending_complete →
                        # progress → never trips). Observe before the end-of-turn
                        # handling so a detected loop wins over a nudge.
                        # A complete_step turn is fed to the guard EXACTLY ONCE by
                        # validate_output_or_nudge below. Skip the flush-observe
                        # here so a malformed complete_step turn is not observed a
                        # second time (double-feed would skew no-progress counters).
                        flushed = _flush_pending()
                        if flushed is not None and not state.complete_step_called:
                            loop_err = guard.observe(flushed)
                            if loop_err is not None:
                                yield perr.make_error_event(
                                    loop_err, kind=getattr(loop_err, "kind", ""))
                                return

                        if handoff_phase == "instruction_sent":
                            # The handoff-writing turn completed: signal the
                            # captured handoff and stop. drive_invocation maps
                            # this to AgentRunResult.handoff_path; the engine
                            # stashes the transcript and reincarnates the session
                            # seeded from the note. Wins over a stray
                            # complete_step on this turn.
                            yield {"type": "handoff",
                                   "payload": {"handoff_path": str(handoff_path)}}
                            return

                        # End-of-turn:
                        #   a) complete_step captured → emit agent_output, return
                        #   b) nudge via continuation_fn → send another query
                        #   c) no complete_step and no nudge → error
                        if state.complete_step_called:
                            res = validate_output_or_nudge(
                                state.complete_step_args,
                                invocation.agent_def.output_schema, guard)
                            if isinstance(res, Ok):
                                # Observe the flush-skipped TurnRecord so co-turn
                                # tool outcomes remain visible to the guard. Result
                                # is safely discarded: the invocation returns
                                # immediately and guard state is thrown away.
                                if flushed is not None:
                                    guard.observe(flushed)
                                if res.recovered is not None:
                                    ev = make_recovery_event(res)
                                    elog.append(agent_id=aid, type=ev["type"],
                                                payload=ev["payload"])
                                yield {"type": "agent_output",
                                       "payload": res.payload}
                                return
                            if isinstance(res, Nudge):
                                ev = make_nudge_event(res)
                                elog.append(agent_id=aid, type=ev["type"],
                                            payload=ev["payload"])
                                # Re-query the SAME session with the nudge so the
                                # agent re-emits. Reset capture and interrupt-preempt
                                # flag so next turn's complete_step is detected fresh.
                                state.complete_step_called = False
                                state.complete_step_args = {}
                                self._complete_requested = False
                                nudge_msg = Message(
                                    role="user",
                                    content=(TextBlock(text=res.message),))
                                messages.append(nudge_msg)
                                transcript.append(nudge_msg)
                                elog.append_user_message(
                                    agent_id=aid, message=nudge_msg, human=False)
                                next_query = res.message
                                requery = True
                                break
                            # LoopError: pause-to-human.
                            yield perr.make_error_event(
                                res, kind=getattr(res, "kind", ""))
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
                             "stderr_tail": self._stderr_sink.tail()},
                            config_dir=config_dir)
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
                            # Carry the offending prose so the narrative can
                            # match-and-drop the held auth text (same [:500]
                            # truncation the _termination_from_result path uses).
                            classified.detail["last_agent_text"] = last_text[:500]
                            _roll_back_error_turn(messages, transcript)
                            yield perr.make_error_event(
                                classified, kind=getattr(classified, "kind", ""))
                            return

                        if invocation.subagent_depth > 0:
                            final_text = last_assistant_text(messages)
                            if final_text:
                                yield {"type": "agent_output",
                                       "payload": {"text": final_text}}
                            return

                        cont = invocation.continuation_fn
                        if cont is not None:
                            next_text, human = cont(last_text)
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
                            user_msg = Message(
                                role="user", content=(TextBlock(text=next_text),),
                            )
                            messages.append(user_msg)
                            transcript.append(user_msg)
                            elog.append_user_message(agent_id=aid, message=user_msg, human=human)
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
                                    agent_id=aid, message=interject_msg, human=True)
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
        reaping a D-state child would re-block the executor, so never await wait()."""
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


# Pinned to undated major.minor ids: they track dated snapshots of that minor
# but never jump to a new major/minor release (a bare "sonnet" alias would).
TIER_DEFAULTS: dict[str, str] = {
    "low": "claude-haiku-4-5",
    "medium": "claude-sonnet-4-6",
    "high": "claude-opus-4-8[1m]",
}
ENV_VAR: str | None = None  # CLI-authenticated (claude login); no env var


def _env_check() -> bool:
    return shutil.which("claude") is not None


def _factory(model: str) -> Provider:
    return ClaudeCodeProvider(model_name=model)
