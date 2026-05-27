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
from imp.agents.transcript import Transcript
from imp.engine.schema import validate_agent_output
from imp.providers._cc_builtins import ALL_CLAUDE_CODE_BUILTIN_TOOLS
from imp.providers.base import Provider

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


# ── Tool registry shimming ───────────────────────────────────────────────────


def _build_tools(invocation: AgentInvocation) -> dict[str, Tool]:
    """Same filtering as anthropic._build_tools; kept verbatim for parity."""
    reg = get_registry()
    out: dict[str, Tool] = {}
    for name in invocation.agent_def.tools:
        if name == "complete_step":
            continue
        if name == "ask_user" and not invocation.interactive:
            continue
        if name == "task":
            continue
        if name not in reg:
            raise KeyError(f"agent declares unknown tool {name!r}")
        out[name] = reg[name]
    if invocation.interactive and "ask_user" in invocation.agent_def.tools:
        out["ask_user"] = reg["ask_user"]
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
    """{param_name: python_type} mapping consumed by `claude_agent_sdk.tool`.

    Defensive: a tool with no parameters still needs *some* schema dict; the
    SDK accepts an empty dict.
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
    schema: Any = _python_schema_for_tool(t)

    async def _impl(args: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "task":
                result = await dispatch_task(
                    ctx, args["subagent_name"], args["task_description"],
                )
                return _tool_response(result, is_error=False)
            if name == "complete_step":
                # Capture the args; return a "success" body so claude proceeds
                # to ResultMessage cleanly. The runner sees the captured args
                # after the next yielded message and emits agent_output.
                state.complete_step_called = True
                state.complete_step_args = args
                return _tool_response("step complete", is_error=False)
            raw = t.invoke(ctx, args)
            content_str = raw if isinstance(raw, str) else json.dumps(raw, default=str)
            return _tool_response(content_str, is_error=False)
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
    """
    return _AsyncIter(_history_dicts(messages))


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


def _join_text(content: tuple[Any, ...]) -> str:
    return "".join(b.text for b in content if isinstance(b, TextBlock))


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


def _sdk_message_to_imp(sdk_msg: Any) -> tuple[Message | None, list[str]]:
    """Convert an SDK AssistantMessage / UserMessage / SystemMessage to imp.

    Returns (Message, warnings). For SDK message types we don't translate
    (ResultMessage, StreamEvent, RateLimitEvent), returns (None, []).

    Unknown content block types become warnings — same contract as the
    Anthropic and OpenAI runners.
    """
    cls_name = type(sdk_msg).__name__
    if cls_name == "AssistantMessage":
        blocks: list[Any] = []
        warnings: list[str] = []
        for b in sdk_msg.content:
            btype = type(b).__name__
            if btype == "TextBlock":
                blocks.append(TextBlock(text=b.text))
            elif btype == "ToolUseBlock":
                blocks.append(ToolUseBlock(
                    id=b.id, name=b.name, input=getattr(b, "input", {}),
                ))
            elif btype == "ThinkingBlock":
                warnings.append(
                    f"skipped thinking block ({len(getattr(b, 'thinking', '') or '')} chars)"
                )
            else:
                warnings.append(f"skipped unknown SDK block type {btype!r}")
        return Message(role="assistant", content=tuple(blocks)), warnings
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
        return Message(role="user", content=tuple(blocks)), warnings
    if cls_name == "SystemMessage":
        # Skip; system messages from claude are init-banner type metadata.
        return None, []
    return None, []


def _serialize(msg: Message) -> dict[str, Any]:
    return {
        "role": msg.role,
        "content": [
            {"type": "text", "text": b.text} if isinstance(b, TextBlock)
            else {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
            if isinstance(b, ToolUseBlock)
            else {"type": "tool_result", "tool_use_id": b.tool_use_id,
                  "content": b.content, "is_error": b.is_error}
            for b in msg.content
        ],
    }


class ClaudeCodeAgentRunner:
    """Drives `claude -p` via claude_agent_sdk.

    Persistence: per-step transcript.jsonl, identical to the other runners.
    Resume: stream-json history replay via _stream_history (Spike 7 verified).
    Tools: in-process MCP server (`create_sdk_mcp_server`) hosts the imp
           registry; the agent sees them as `mcp__imp__<name>`.
    """

    def __init__(self, model_name: str, *, client_factory: Any = None) -> None:
        self.model = model_name
        self._client_factory = client_factory
        self._client: Any = None
        self._cancel = False

    def _build_options(self, invocation: AgentInvocation, mcp_server: Any,
                       tool_names: list[str]) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions
        return ClaudeAgentOptions(
            model=self.model,
            system_prompt=invocation.system_prompt,
            mcp_servers={"imp": mcp_server},
            allowed_tools=[f"mcp__imp__{n}" for n in tool_names],
            disallowed_tools=list(ALL_CLAUDE_CODE_BUILTIN_TOOLS),
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

        async def _spawn(sub_invocation: AgentInvocation) -> AsyncIterator[Event]:
            child = ClaudeCodeAgentRunner(
                model_name=sub_invocation.model_name,
                client_factory=client_factory,
            )
            return child.run(sub_invocation)

        return _spawn

    async def run(self, invocation: AgentInvocation) -> AsyncGenerator[Event, None]:
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

        state = _RunState()
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
            resume_state = classify_resume(messages, interactive=invocation.interactive)
            if resume_state is ResumeState.DANGLING_TOOL_USE:
                yield {"type": "error", "payload": {
                    "kind": "transcript_invalid",
                    "message": "transcript ended with dangling tool_use",
                }}
                return
            if resume_state is ResumeState.MISSING_COMPLETE_STEP:
                yield {"type": "error", "payload": {
                    "kind": "missing_complete_step",
                    "message": "autonomous agent transcript ended without complete_step",
                }}
                return
            # TODO(plan-3): PROMPT_USER finalization same as the other runners.
        else:
            messages = []
            for m in invocation.initial_messages:
                messages.append(m)
                transcript.append(m)
            tool_calls_used = 0

        ceiling = invocation.max_tool_calls or 50

        client_ctx = self._build_client(opts)
        async with client_ctx as client:
            await client.query(_stream_history(messages))

            async for sdk_msg in client.receive_response():
                if self._cancel:
                    yield {"type": "error", "payload": {
                        "kind": "interrupted",
                        "message": "runner cancelled by interrupt",
                    }}
                    return

                cls_name = type(sdk_msg).__name__
                imp_msg, warns = _sdk_message_to_imp(sdk_msg)
                for w in warns:
                    yield {"type": "runner_warning", "payload": {"message": w}}
                if imp_msg is not None:
                    messages.append(imp_msg)
                    transcript.append(imp_msg)

                    if imp_msg.role == "assistant":
                        yield {"type": "agent_message",
                               "payload": _serialize(imp_msg)}
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
                                    yield {"type": "tool_result", "payload": {
                                        "id": b.id,
                                        "content": ceil_msg,
                                        "error": True,
                                    }}
                                    continue
                                tool_calls_used += 1
                                yield {"type": "tool_call", "payload": {
                                    "id": b.id, "name": b.name, "input": b.input,
                                }}
                    elif imp_msg.role == "user":
                        # ToolResultBlocks generated by claude calling our MCP
                        # tools. Emit the corresponding tool_result events.
                        for b in imp_msg.content:
                            if isinstance(b, ToolResultBlock):
                                payload: dict[str, Any] = {
                                    "id": b.tool_use_id, "content": b.content,
                                }
                                if b.is_error:
                                    payload["error"] = True
                                yield {"type": "tool_result", "payload": payload}

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
                                "kind": "output_validation",
                                "message": str(ve),
                            }}
                            return
                        yield {"type": "agent_output",
                               "payload": state.complete_step_args}
                        return

                    if tool_calls_used >= ceiling and not state.complete_step_called:
                        yield {"type": "error", "payload": {
                            "kind": "tool_call_ceiling_exceeded",
                            "ceiling": ceiling,
                        }}
                        return

                    if invocation.interactive and ctx.user_input_fn is not None:
                        user_text = ctx.user_input_fn("")
                        user_msg = Message(
                            role="user", content=(TextBlock(text=user_text),),
                        )
                        messages.append(user_msg)
                        transcript.append(user_msg)
                        yield {"type": "user_input",
                               "payload": {"text": user_text}}
                        await client.query(user_text)
                        continue

                    yield {"type": "error", "payload": {
                        "kind": "no_complete_step",
                        "message": "agent finished without calling complete_step",
                    }}
                    return

        # Fell out of the receive loop without seeing a ResultMessage.
        yield {"type": "error", "payload": {
            "kind": "no_result_message",
            "message": "SDK closed without yielding a ResultMessage",
        }}

    async def interrupt(self) -> None:
        self._cancel = True


class ClaudeCodeProvider:
    name = "claude_code"

    def __init__(self, model_name: str, *, client_factory: Any = None) -> None:
        self.model = model_name
        self._client_factory = client_factory

    def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
        return ClaudeCodeAgentRunner(
            model_name=self.model, client_factory=self._client_factory,
        )


def _env_check() -> bool:
    return shutil.which("claude") is not None


def _factory(model: str) -> Provider:
    return ClaudeCodeProvider(model_name=model)
