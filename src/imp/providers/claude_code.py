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

`extra_args={"no-session-persistence": None}` is the documented forwarding
path for `--no-session-persistence` (spec §6.11; verified in Spike 7 and
SDK `_internal/transport/subprocess_cli.py`).
"""

from __future__ import annotations

import inspect
import json
import shutil
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from imp.agents.classify import ResumeState, classify_resume, count_tool_uses
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
from imp.agents.subagents import dispatch_task, make_task_tool
from imp.agents.tools import Tool, get_registry
from imp.agents.tools.complete_step import make_complete_step_tool
from imp.agents.tools.context import ToolContext
from imp.agents.tools.errors import FatalToolError
from imp.agents.transcript import (
    Transcript,
    balance_success_on_resume,
    repair_dangling_tool_use_on_resume,
)
from imp.engine.schema import validate_agent_output
from imp.providers import errors as perr
from imp.providers._cc_builtins import ALL_CLAUDE_CODE_BUILTIN_TOOLS
from imp.providers.base import Provider

_AUTH_PATTERNS = (
    "Invalid API key",
    "Please run `/login`",
)

_QUOTA_PATTERNS = (
    "You have reached your usage limit",
    "Credit balance is too low",
)


def _classify_agent_text(text: str) -> perr.ProviderError | None:
    """Detect known claude CLI terminal-error patterns in agent text.

    The CLI catches its own errors and returns them as a text response
    with exit 0 (no out-of-band signal). Exact-substring patterns,
    conservatively chosen: false negatives fall back to existing
    retry behavior; false positives would convert recoverable agent
    confusion into fatal pause.
    """
    for pat in _AUTH_PATTERNS:
        if pat in text:
            return perr.AuthError(f"claude CLI: {pat}")
    for pat in _QUOTA_PATTERNS:
        if pat in text:
            return perr.QuotaError(f"claude CLI: {pat}")
    return None


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


# ── Tool registry shimming ───────────────────────────────────────────────────


def _build_tools(invocation: AgentInvocation) -> dict[str, Tool]:
    """Same filtering as anthropic._build_tools; kept verbatim for parity."""
    reg = get_registry()
    out: dict[str, Tool] = {}
    for name in invocation.agent_def.tools:
        if name == "complete_step":
            continue
        if name == "task":
            continue
        if name not in reg:
            raise KeyError(f"agent declares unknown tool {name!r}")
        out[name] = reg[name]
    if "task" in invocation.agent_def.tools:
        out["task"] = make_task_tool()
    out["complete_step"] = make_complete_step_tool(invocation.agent_def.output_schema)
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
        try:
            if name == "task":
                # Append the task tool_call event INSIDE _impl using the
                # synchronously-available __tool_use_id__. This is race-free:
                # the receive loop processes AssistantMessage on a different
                # async path and would create a duplicate if it also appended
                # here. The receive loop SKIPS task tool_calls (see below).
                tu_id = args.get("__tool_use_id__", "")
                tc_event_id = ""
                # ctx.event_log is always set by _run_loop before tools dispatch;
                # this guard is defensive (no-log → tool_call_event_id="" →
                # subagent agent_id="sub:" with a broken parent link).
                if ctx.event_log is not None:
                    tc_ev = ctx.event_log.append_tool_call(
                        agent_id=state.agent_id,
                        block=ToolUseBlock(
                            id=tu_id, name="task",
                            input={"subagent_name": args["subagent_name"],
                                   "task_description": args["task_description"]}))
                    tc_event_id = tc_ev["id"]
                result = await dispatch_task(
                    ctx, args["subagent_name"], args["task_description"],
                    tool_call_event_id=tc_event_id,
                )
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
            text = f"{type(exc).__name__}: {exc}"
            state.runtime_errors.append(text)
            return _tool_response(text, is_error=True)

    return sdk_tool(name, t.description, schema)(_impl)


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
    assistant turns ("tool use concurrency issues"), so we fold streamed
    fragments back into one assistant message per logical turn before replay.
    Drop empty assistant content (thinking-only fragments after we extracted
    the thinking blocks) since "assistant with no content" is also invalid.
    """
    return _AsyncIter(_history_dicts(_merge_assistant_fragments(messages)))


def _merge_assistant_fragments(messages: list[Message]) -> list[Message]:
    out: list[Message] = []
    for m in messages:
        if m.role == "assistant" and not m.content:
            continue
        if m.role == "assistant" and out and out[-1].role == "assistant":
            out[-1] = Message(
                role="assistant",
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
    text-only turn (interactive resume state), the runner has to send a
    fresh user prompt first (handled in the runner above the stream).
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

    def _build_options(self, invocation: AgentInvocation, mcp_server: Any,
                       tool_names: list[str]) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions
        # ToolSearch is claude-p's internal schema-loader for deferred MCP
        # tools (default mode for any mcp_server). It only returns schemas;
        # it can't read/write/exec, so allowing it doesn't break the
        # strict-MCP-only stance. Disallowing it breaks resume: when claude-p
        # replays an MCP tool_use from history it tries to ToolSearch the
        # schema first, and a denied tool_use shows up at the API as a
        # tool_use without a matching tool_result ("API Error: 400 due to
        # tool use concurrency issues"). Keep ToolSearch out of the deny
        # list while keeping every other built-in denied.
        disallowed = [t for t in ALL_CLAUDE_CODE_BUILTIN_TOOLS if t != "ToolSearch"]
        return ClaudeAgentOptions(
            model=self.model,
            # Pin the `claude` subprocess to the change's project_root. Without
            # this the SDK launches claude with cwd=None and leaves $PWD as
            # inherited, so claude keys off a stale $PWD and edits/commits the
            # wrong working tree (e.g. imp's own repo when launched from a
            # subdir or via Popen(cwd=...) which doesn't update $PWD).
            cwd=str(invocation.change_context.project_root),
            system_prompt=invocation.system_prompt,
            mcp_servers={"imp": mcp_server},
            allowed_tools=[f"mcp__imp__{n}" for n in tool_names],
            disallowed_tools=disallowed,
            setting_sources=[],
            strict_mcp_config=True,
            extra_args={"no-session-persistence": None},
        )

    def _build_client(self, opts: Any) -> Any:
        """Return an async-context-manager wrapping ClaudeSDKClient(opts).
        Tests inject a factory that returns a FakeClaudeSDKClient."""
        if self._client_factory is not None:
            return self._client_factory(opts)
        from claude_agent_sdk import ClaudeSDKClient
        return ClaudeSDKClient(opts)

    def _make_spawn_subagent(self) -> Any:
        client_factory = self._client_factory
        user_input_fn = self._user_input_fn

        async def _spawn(sub_invocation: AgentInvocation) -> AsyncIterator[Event]:
            child = ClaudeCodeAgentRunner(
                model_name=sub_invocation.model_name,
                client_factory=client_factory,
                user_input_fn=user_input_fn,
            )
            return child.run(sub_invocation)

        return _spawn

    async def run(self, invocation: AgentInvocation) -> AsyncGenerator[Event, None]:
        try:
            async for ev in self._run_loop(invocation):
                yield ev
        except perr.ProviderError as exc:
            yield perr.make_error_event(exc)
            return
        except Exception as exc:
            yield perr.make_unknown_error_event(exc)
            return

    async def _run_loop(self, invocation: AgentInvocation) -> AsyncGenerator[Event, None]:
        from imp.engine.state import JsonlEventLog
        elog = invocation.event_log or JsonlEventLog(invocation.events_path)
        aid = invocation.agent_id or "step:unnamed"

        transcript = Transcript(invocation.transcript_dir / "transcript.jsonl")
        tools = _build_tools(invocation)
        ctx = ToolContext(
            project_root=invocation.change_context.project_root,
            artifacts_dir=invocation.change_context.artifacts_dir,
            fs_permissions=invocation.fs_permissions,
            events_path=invocation.events_path,
            subagent_defs=invocation.subagent_defs,
            provider_name=invocation.provider_name,
            model_name=invocation.model_name,
            parent_transcript_dir=invocation.transcript_dir,
            interactive=invocation.interactive,
        )
        ctx.change_context = invocation.change_context
        ctx.spawn_subagent = self._make_spawn_subagent()
        ctx.event_log = elog
        if self._user_input_fn is not None:
            ctx.user_input_fn = self._user_input_fn
        self._ctx = ctx

        state = _RunState()
        state.agent_id = aid
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

        # Build options only when we'll use the real SDK (mcp_server is not None).
        # When a client_factory is injected (tests), skip building options to
        # avoid constructing ClaudeAgentOptions with mcp_server=None.
        if mcp_server is not None:
            opts = self._build_options(invocation, mcp_server, list(tools))
        else:
            opts = None

        existing = transcript.load()
        if existing:
            messages = existing
            tool_calls_used = count_tool_uses(messages)
            # Mid-tool Ctrl+C (spec §6.15) can leave a dangling tool_use.
            # Persist a synthetic tool_result so the SEND_TO_LLM path has a
            # balanced transcript; emit `tool_result` events so `imp log`
            # shows what the agent sees on the next turn.
            if invocation.balance_on_resume:
                balance_success_on_resume(
                    messages, transcript, elog, aid,
                    wrapper_text=invocation.retry_feedback)
            else:
                repair = repair_dangling_tool_use_on_resume(messages, transcript)
                if repair is not None:
                    for b in repair.content:
                        if isinstance(b, ToolResultBlock):
                            elog.append_tool_result(agent_id=aid, block=b)
            resume_state = classify_resume(messages, interactive=invocation.interactive)
            if resume_state is ResumeState.DANGLING_TOOL_USE:
                yield {"type": "error", "payload": {
                    "category": "config",
                    "kind": "transcript_invalid",
                    "message": "transcript ended with dangling tool_use",
                }}
                return
            if resume_state is ResumeState.MISSING_COMPLETE_STEP:
                last_text = ""
                if messages and messages[-1].role == "assistant":
                    last_text = _text_of(messages[-1])
                classified = _classify_agent_text(last_text)
                if classified is not None:
                    yield perr.make_error_event(classified)
                    return
                yield {"type": "error", "payload": {
                    "category": "agent_output_missing",
                    "kind": "missing_complete_step",
                    "message": "autonomous agent transcript ended without complete_step",
                    "detail": {"last_agent_text": last_text[:500]},
                }}
                return
            if resume_state is ResumeState.PROMPT_USER:
                if ctx.user_input_fn is None:
                    yield {"type": "error", "payload": {
                        "category": "config",
                        "kind": "no_user_input_fn",
                        "message": (
                            "PROMPT_USER resume requires user_input_fn; "
                            "none installed by runner"
                        ),
                    }}
                    return
                user_text = ctx.user_input_fn("")
                if inspect.isawaitable(user_text):
                    user_text = await user_text
                user_msg = Message(
                    role="user", content=(TextBlock(text=user_text),),
                )
                messages.append(user_msg)
                transcript.append(user_msg)
                elog.append_user_message(agent_id=aid, message=user_msg)
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
                params={"no-session-persistence": None},
                max_tool_calls=invocation.max_tool_calls or 50)
            for m in invocation.initial_messages:
                if all(isinstance(b, TextBlock) for b in m.content):
                    elog.append_user_message(agent_id=aid, message=m)

        ceiling = invocation.max_tool_calls or 50

        client_ctx = self._build_client(opts)
        async with client_ctx as client:
            await client.query(_stream_history(messages))

            async for sdk_msg in client.receive_response():
                if self._cancel:
                    yield {"type": "error", "payload": {
                        "category": "config",
                        "kind": "interrupted",
                        "message": "runner cancelled by interrupt",
                    }}
                    return

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
                        if cls_name == "AssistantMessage":
                            raw = _serialize_claude_code_message(sdk_msg)
                            elog.append_llm_response(
                                agent_id=aid, provider=invocation.provider_name,
                                model=self.model, raw=raw,
                                usage=raw.get("usage") or {},
                                stop_reason=raw.get("stop_reason"))
                        for b in imp_msg.content:
                            if isinstance(b, ToolUseBlock):
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
                                if tool_calls_used >= ceiling:
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
                                if canon == "task":
                                    # tool_call for task is appended inside the
                                    # MCP wrapper (_impl) using __tool_use_id__,
                                    # race-free. Don't double-append here.
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
                                elog.append_tool_result(agent_id=aid, block=b)

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
                    # End-of-turn:
                    #   a) complete_step captured → emit agent_output, return
                    #   b) interactive mode → prompt user, send another query
                    #   c) autonomous mode without complete_step → error
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

                    if tool_calls_used >= ceiling and not state.complete_step_called:
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

                    # Classify ONLY on the non-interactive path, before the
                    # nudge: a surfaced provider error (auth/quota) must win
                    # over a pointless nudge. Never classify an interactive
                    # turn — its prose can legitimately contain auth/quota
                    # substrings (see _classify_agent_text docstring). Note:
                    # this now runs on each non-interactive nudge turn (up to
                    # N+1), not just the terminal one — intentional, so an auth
                    # failure surfaces on turn 1 instead of after N nudges.
                    if not invocation.interactive:
                        classified = _classify_agent_text(last_text)
                        if classified is not None:
                            yield perr.make_error_event(classified)
                            return

                    cont = invocation.continuation_fn
                    if cont is not None:
                        next_text = cont(last_text)
                        if inspect.isawaitable(next_text):
                            next_text = await next_text
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
                        await client.query(next_text)
                        continue

                    # Legacy/test fallback: no continuation_fn wired.
                    if invocation.interactive and ctx.user_input_fn is not None:
                        user_text = ctx.user_input_fn("")
                        if inspect.isawaitable(user_text):
                            user_text = await user_text
                        user_msg = Message(
                            role="user", content=(TextBlock(text=user_text),),
                        )
                        messages.append(user_msg)
                        transcript.append(user_msg)
                        elog.append_user_message(agent_id=aid, message=user_msg)
                        await client.query(user_text)
                        continue

                    yield {"type": "error", "payload": {
                        "category": "agent_output_missing",
                        "kind": "no_complete_step",
                        "message": "agent finished without calling complete_step",
                        "detail": {"last_agent_text": last_text[:500]},
                    }}
                    return

            # receive_response() exhausted without a ResultMessage. Capture
            # subprocess diagnostics here, while the transport is still live
            # (before __aexit__ runs disconnect() and nulls _transport).
            # Against today's SDK SubprocessCLITransport does not expose
            # returncode/stderr_tail, so this returns {}; the capture is
            # forward-compatible for when the SDK adds those attributes.
            self._last_no_result_diag = _subprocess_diag(client)

        # Fell out of the async-with block without yielding a ResultMessage.
        # Use the diagnostics captured inside the block (transport was live).
        # Classification stays `network` (retryable).
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

    async def interrupt(self) -> None:
        from imp.agents.tools.context import terminate_running_processes
        self._cancel = True
        if self._ctx is not None:
            await terminate_running_processes(self._ctx, timeout=2.0)


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
