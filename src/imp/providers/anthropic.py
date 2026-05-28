"""Anthropic provider + runner (spec §6.9). Direct anthropic.AsyncAnthropic.

Loop:
  1. Build messages (transcript or initial_messages)
  2. POST to messages.create with declared tools
  3. Append assistant response to transcript; emit agent_message
  4. For each tool_use block: dispatch tool, append tool_result, emit events
  5. complete_step catches StepComplete → validate, emit agent_output, return
  6. No tool_use in assistant and interactive: prompt user; else error
"""

from __future__ import annotations

import inspect
import json
import os
from collections.abc import AsyncGenerator, AsyncIterator
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
from imp.agents.tools.complete_step import StepComplete, make_complete_step_tool
from imp.agents.tools.context import ToolContext
from imp.agents.tools.errors import FatalToolError
from imp.agents.transcript import Transcript, repair_dangling_tool_use_on_resume
from imp.engine.schema import validate_agent_output
from imp.providers import errors as perr
from imp.providers.base import Provider


def _classify_anthropic_exception(exc: BaseException) -> perr.ProviderError:
    """Translate an Anthropic SDK exception into the matching
    ProviderError subclass. Re-raises anything that doesn't fit a
    known category — the runner's top-level handler turns that into
    an 'unknown' error event.
    """
    from anthropic import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AuthenticationError,
        BadRequestError,
        InternalServerError,
        NotFoundError,
        PermissionDeniedError,
    )
    from anthropic import RateLimitError as _AnthropicRateLimit

    if isinstance(exc, AuthenticationError):
        request_id = getattr(exc, "request_id", None)
        detail: dict[str, object] = {"request_id": request_id} if request_id else {}
        return perr.AuthError(str(exc), detail=detail)
    if isinstance(exc, PermissionDeniedError):
        return perr.AuthError(str(exc))
    if isinstance(exc, _AnthropicRateLimit):
        retry_after: float | None = None
        resp = getattr(exc, "response", None)
        if resp is not None:
            headers = getattr(resp, "headers", None) or {}
            raw = headers.get("retry-after") if hasattr(headers, "get") else None
            if raw:
                try:
                    retry_after = float(raw)
                except (TypeError, ValueError):
                    retry_after = None
        detail = (
            {"retry_after_seconds": retry_after} if retry_after is not None else {}
        )
        return perr.RateLimitError(str(exc), detail=detail)
    if isinstance(exc, (APIConnectionError, APITimeoutError, InternalServerError)):
        return perr.NetworkError(str(exc))
    if isinstance(exc, NotFoundError):
        return perr.ConfigError(f"model not found: {exc}")
    if isinstance(exc, BadRequestError):
        msg = str(exc)
        msg_low = msg.lower()
        if "prompt is too long" in msg or "context_length_exceeded" in msg:
            return perr.ContextWindowError(msg)
        if "credit balance" in msg_low or "billing" in msg_low:
            return perr.QuotaError(msg)
        return perr.ConfigError(msg)
    if isinstance(exc, APIStatusError):
        return perr.NetworkError(f"{exc.status_code}: {exc}")
    raise exc


def _build_tools(invocation: AgentInvocation) -> dict[str, Tool]:
    """Build the per-step tool dict.

    `task` is added when the agent declares subagents — note it bypasses the
    runner's normal Tool.invoke path (see dispatch loop below).
    """
    reg = get_registry()
    out: dict[str, Tool] = {}
    for name in invocation.agent_def.tools:
        if name == "complete_step":
            continue
        if name == "ask_user" and not invocation.interactive:
            continue  # filtered
        if name == "task":
            # Handled specially below; do NOT pull from registry.
            continue
        if name not in reg:
            raise KeyError(f"agent declares unknown tool {name!r}")
        out[name] = reg[name]
    if invocation.interactive and "ask_user" in invocation.agent_def.tools:
        out["ask_user"] = reg["ask_user"]
    if "task" in invocation.agent_def.tools:
        # Always register `task` if the agent declares it. If subagent_defs is
        # empty, dispatch_task raises a clear "subagent X not declared" error
        # the first time the LLM invokes `task` — much better than silently
        # advertising no tool at all and letting the LLM fumble.
        out["task"] = make_task_tool()
    out["complete_step"] = make_complete_step_tool(invocation.agent_def.output_schema)
    return out


def _anthropic_tool_schema(t: Tool) -> dict[str, Any]:
    return {"name": t.name, "description": t.description, "input_schema": t.input_schema}


def _message_to_anthropic(msg: Message) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for b in msg.content:
        if isinstance(b, TextBlock):
            content.append({"type": "text", "text": b.text})
        elif isinstance(b, ToolUseBlock):
            content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif isinstance(b, ToolResultBlock):
            content.append({
                "type": "tool_result", "tool_use_id": b.tool_use_id,
                "content": b.content, "is_error": b.is_error,
            })
    return {"role": msg.role, "content": content}


def _serialize_anthropic_response(response: Any) -> dict[str, Any]:
    """Serialize a provider response to a plain dict (the llm_response.raw
    value). Prefers SDK pydantic .model_dump() for max fidelity; falls back to
    introspecting .content / .stop_reason / .usage for test fakes. Both paths
    produce {content: [block-dicts], stop_reason, usage}."""
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except TypeError:
            return dump()
    content: list[dict[str, Any]] = []
    for block in getattr(response, "content", []):
        kind = getattr(block, "type", None)
        if kind == "text":
            content.append({"type": "text", "text": block.text})
        elif kind == "tool_use":
            content.append({"type": "tool_use", "id": block.id,
                            "name": block.name, "input": block.input})
        elif kind == "thinking":
            content.append({"type": "thinking",
                            "thinking": getattr(block, "thinking", "")})
        else:
            content.append({"type": str(kind)})
    return {
        "content": content,
        "stop_reason": getattr(response, "stop_reason", None),
        "usage": _usage_dict(getattr(response, "usage", None)),
    }


def _usage_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    dump = getattr(usage, "model_dump", None)
    if callable(dump):
        return dump()
    if isinstance(usage, dict):
        return usage
    return {k: getattr(usage, k) for k in
            ("input_tokens", "output_tokens",
             "cache_read_input_tokens", "cache_creation_input_tokens")
            if getattr(usage, k, None) is not None}


def _parse_anthropic_message(raw: dict[str, Any]) -> Message:
    """raw → assistant Message. Reconstruction-critical; shared with replay.py.
    Thinking + unknown blocks are dropped (they're not conversation history)."""
    content: list[Any] = []
    for block in raw.get("content", []):
        kind = block.get("type")
        if kind == "text":
            content.append(TextBlock(text=block.get("text", "")))
        elif kind == "tool_use":
            content.append(ToolUseBlock(id=block["id"], name=block["name"],
                                        input=block.get("input", {})))
        # thinking + unknown: skipped (see diagnostics)
    return Message(role="assistant", content=tuple(content))


def _extract_anthropic_diagnostics(raw: dict[str, Any]) -> Diagnostics:
    """Render-only: thinking contents + unknown-block warnings."""
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


class AnthropicAgentRunner:
    def __init__(
        self, model_name: str,
        *,
        client_factory: Any = None,
        user_input_fn: Any = None,             # NEW
    ) -> None:
        self.model = model_name                     # public per Provider protocol
        self._client_factory = client_factory
        self._user_input_fn = user_input_fn    # NEW
        self._client: Any = None
        self._cancel = False
        self._ctx: ToolContext | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                import anthropic
                self._client = anthropic.AsyncAnthropic()
        return self._client

    def _make_spawn_subagent(self) -> Any:
        """Closure that creates a same-class child runner for the task tool.

        Returns an async callable that takes a sub_invocation and returns the
        child runner's event AsyncIterator. Same provider, same model — V1
        constraint per spec §6.13.
        """
        client_factory = self._client_factory

        async def _spawn(sub_invocation: AgentInvocation) -> AsyncIterator[Event]:
            child = AnthropicAgentRunner(
                model_name=sub_invocation.model_name, client_factory=client_factory,
            )
            return child.run(sub_invocation)

        return _spawn

    async def _close_client(self) -> None:
        """Close the underlying SDK client if we own it and it supports close().

        Real `anthropic.AsyncAnthropic` owns an httpx pool; without explicit
        close() the pool's async teardown can race with pytest-anyio's
        per-test loop teardown and surface "Event loop is closed" errors in
        later tests. Test fakes don't expose `.close()` — skip safely.
        """
        import contextlib
        if self._client is None:
            return
        close = getattr(self._client, "close", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()
        self._client = None

    async def run(self, invocation: AgentInvocation) -> AsyncGenerator[Event, None]:
        try:
            try:
                async for ev in self._run_loop(invocation):
                    yield ev
            except perr.ProviderError as exc:
                yield perr.make_error_event(exc)
                return
            except Exception as exc:
                try:
                    classified = _classify_anthropic_exception(exc)
                except Exception:
                    yield perr.make_unknown_error_event(exc)
                    return
                yield perr.make_error_event(classified)
                return
        finally:
            await self._close_client()

    async def _run_loop(
        self, invocation: AgentInvocation,
    ) -> AsyncGenerator[Event, None]:
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
        ctx.change_context = invocation.change_context  # for subagent dispatch
        ctx.spawn_subagent = self._make_spawn_subagent()
        if self._user_input_fn is not None:
            ctx.user_input_fn = self._user_input_fn  # NEW
        self._ctx = ctx

        existing = transcript.load()
        if existing:
            messages = existing
            tool_calls_used = count_tool_uses(messages)
            # Mid-tool Ctrl+C (spec §6.15) can leave a dangling tool_use when
            # the executor's pause check fires after `tool_result` was yielded
            # but before the user message was appended. Patch the transcript
            # before classifying.
            repair = repair_dangling_tool_use_on_resume(messages, transcript)
            if repair is not None:
                for b in repair.content:
                    if isinstance(b, ToolResultBlock):
                        yield {"type": "tool_result", "payload": {
                            "id": b.tool_use_id,
                            "content": b.content,
                            "error": b.is_error,
                        }}
            state = classify_resume(messages, interactive=invocation.interactive)
            if state is ResumeState.DANGLING_TOOL_USE:
                yield {"type": "error", "payload": {
                    "category": "config",
                    "kind": "transcript_invalid",
                    "message": "transcript ended with dangling tool_use",
                }}
                return
            if state is ResumeState.MISSING_COMPLETE_STEP:
                last_text = ""
                if messages and messages[-1].role == "assistant":
                    last_text = "".join(
                        b.text for b in messages[-1].content
                        if isinstance(b, TextBlock)
                    )
                yield {"type": "error", "payload": {
                    "category": "agent_output_missing",
                    "kind": "missing_complete_step",
                    "message": "autonomous agent transcript ended without complete_step",
                    "detail": {"last_agent_text": last_text[:500]},
                }}
                return
            if state is ResumeState.PROMPT_USER:
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
                yield {"type": "user_input", "payload": {"text": user_text}}
        else:
            messages = []
            for m in invocation.initial_messages:
                messages.append(m)
                transcript.append(m)
            tool_calls_used = 0

        # Spec §6.5: agent_def.max_tool_calls or workflow.defaults.max_tool_calls or 50.
        # The engine (Task 28) resolves the first two and passes the result via
        # invocation.max_tool_calls; we default to 50 only if it's still None.
        ceiling = invocation.max_tool_calls or 50

        while not self._cancel:
            anthropic_tools = [_anthropic_tool_schema(t) for t in tools.values()]
            client = self._get_client()
            response = await client.messages.create(
                model=self.model,
                system=invocation.system_prompt,
                tools=anthropic_tools,
                messages=[_message_to_anthropic(m) for m in messages],
                max_tokens=8192,
            )

            raw = _serialize_anthropic_response(response)
            assistant_msg = _parse_anthropic_message(raw)
            diag = _extract_anthropic_diagnostics(raw)
            for content in diag.thinkings:
                yield {"type": "thinking", "payload": {"content": content}}
            for w in diag.block_warnings:
                yield {"type": "runner_warning", "payload": {"message": w}}
            messages.append(assistant_msg)
            transcript.append(assistant_msg)
            yield {"type": "agent_message", "payload": _serialize(assistant_msg)}

            tool_uses = [b for b in assistant_msg.content if isinstance(b, ToolUseBlock)]
            if not tool_uses:
                if invocation.interactive and ctx.user_input_fn is not None:
                    user_text = ctx.user_input_fn("")
                    if inspect.isawaitable(user_text):
                        user_text = await user_text
                    user_msg = Message(role="user", content=(TextBlock(text=user_text),))
                    messages.append(user_msg)
                    transcript.append(user_msg)
                    yield {"type": "user_input", "payload": {"text": user_text}}
                    continue
                last_text = "".join(
                    b.text for b in assistant_msg.content
                    if isinstance(b, TextBlock)
                )
                yield {"type": "error", "payload": {
                    "category": "agent_output_missing",
                    "kind": "no_complete_step",
                    "message": "agent finished without calling complete_step",
                    "detail": {"last_agent_text": last_text[:500]},
                }}
                return

            tool_results: list[ToolResultBlock] = []
            ceiling_tripped = False
            for tu in tool_uses:
                if tool_calls_used >= ceiling:
                    # Backfill error tool_results to preserve transcript invariant.
                    err_text = f"tool_call_ceiling_exceeded (ceiling={ceiling})"
                    tool_results.append(ToolResultBlock(tu.id, err_text, is_error=True))
                    yield {"type": "tool_result", "payload": {
                        "id": tu.id, "content": err_text, "error": True,
                    }}
                    ceiling_tripped = True
                    continue
                tool_calls_used += 1

                yield {"type": "tool_call", "payload": {
                    "id": tu.id, "name": tu.name, "input": tu.input,
                }}

                # task special-case: bypasses the sync Tool.invoke path
                # entirely — go straight to the async dispatcher.
                if tu.name == "task":
                    try:
                        result_str = await dispatch_task(
                            ctx, tu.input["subagent_name"], tu.input["task_description"],
                        )
                    except Exception as e:
                        text = f"{type(e).__name__}: {e}"
                        tool_results.append(ToolResultBlock(tu.id, text, is_error=True))
                        yield {"type": "tool_result", "payload": {
                            "id": tu.id, "content": text, "error": True,
                        }}
                        continue
                    tool_results.append(ToolResultBlock(tu.id, result_str))
                    yield {"type": "tool_result", "payload": {
                        "id": tu.id, "content": result_str,
                    }}
                    continue

                tool = tools.get(tu.name)
                if tool is None:
                    msg = f"unknown tool {tu.name!r}"
                    tool_results.append(ToolResultBlock(tu.id, msg, is_error=True))
                    yield {"type": "tool_result", "payload": {
                        "id": tu.id, "content": msg, "error": True,
                    }}
                    continue
                try:
                    raw = await tool.async_invoke(ctx, tu.input)
                except StepComplete as sc:
                    try:
                        validate_agent_output(sc.args_payload, invocation.agent_def.output_schema)
                    except Exception as ve:
                        yield {"type": "error", "payload": {
                            "category": "agent_output_missing",
                            "kind": "output_validation",
                            "message": str(ve),
                        }}
                        return
                    yield {"type": "agent_output", "payload": sc.args_payload}
                    return
                except FatalToolError as fte:
                    yield {"type": "error", "payload": {
                        "category": "config",
                        "kind": "fatal_tool_error",
                        "tool": tu.name,
                        "message": str(fte),
                    }}
                    return
                except Exception as e:
                    text = f"{type(e).__name__}: {e}"
                    tool_results.append(ToolResultBlock(tu.id, text, is_error=True))
                    yield {"type": "tool_result", "payload": {
                        "id": tu.id, "content": text, "error": True,
                    }}
                    continue

                content_str = raw if isinstance(raw, str) else json.dumps(raw, default=str)
                if tu.name == "bash":
                    # Detect a kill so the CLI can render a tool_killed event.
                    try:
                        parsed = json.loads(content_str)
                    except (TypeError, ValueError):
                        parsed = {}
                    rc = parsed.get("returncode")
                    if isinstance(rc, int) and rc < 0:
                        yield {"type": "tool_killed", "payload": {
                            "id": tu.id, "name": "bash", "signal": -rc,
                        }}
                tool_results.append(ToolResultBlock(tu.id, content_str))
                yield {"type": "tool_result", "payload": {
                    "id": tu.id, "content": content_str,
                }}

            user_msg = Message(role="user", content=tuple(tool_results))
            messages.append(user_msg)
            transcript.append(user_msg)

            if ceiling_tripped:
                yield {"type": "error", "payload": {
                    "category": "config",
                    "kind": "tool_call_ceiling_exceeded",
                    "ceiling": ceiling,
                }}
                return

        # cancelled (interrupt)
        yield {"type": "error", "payload": {
            "category": "config",
            "kind": "interrupted",
            "message": "runner cancelled by interrupt",
        }}

    async def interrupt(self) -> None:
        from imp.agents.tools.context import terminate_running_processes
        self._cancel = True
        if self._ctx is not None:
            await terminate_running_processes(self._ctx, timeout=2.0)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model_name: str, *, client_factory: Any = None) -> None:
        self.model = model_name                     # public per Provider protocol
        self._client_factory = client_factory

    def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
        return AnthropicAgentRunner(
            model_name=self.model, client_factory=self._client_factory,
            user_input_fn=invocation.user_input_fn,
        )


def _env_check() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _factory(model: str) -> Provider:
    return AnthropicProvider(model_name=model)
