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
from imp.agents.transcript import Transcript
from imp.engine.schema import validate_agent_output
from imp.providers.base import Provider


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


def _anthropic_to_message(
    response: Any,
) -> tuple[Message, list[str], list[str]]:
    """Translate an Anthropic response to an imp Message + a list of warning
    strings + a list of extended-thinking contents.

    Thinking blocks are stripped from the visible message (they're not part
    of the conversation history we send back) but their content is returned
    separately so the runner can emit `thinking` events for the audit trail.
    Unknown block types still become warnings.
    """
    content: list[Any] = []
    warnings: list[str] = []
    thinkings: list[str] = []
    for block in response.content:
        kind = getattr(block, "type", None)
        if kind == "text":
            content.append(TextBlock(text=block.text))
        elif kind == "tool_use":
            content.append(ToolUseBlock(id=block.id, name=block.name, input=block.input))
        elif kind == "thinking":
            thinkings.append(str(getattr(block, "thinking", "") or ""))
        else:
            warnings.append(f"skipped unknown block type {kind!r}")
    return Message(role="assistant", content=tuple(content)), warnings, thinkings


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
            async for ev in self._run_loop(invocation):
                yield ev
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
            state = classify_resume(messages, interactive=invocation.interactive)
            if state is ResumeState.DANGLING_TOOL_USE:
                yield {"type": "error", "payload": {
                    "category": "config",
                    "kind": "transcript_invalid",
                    "message": "transcript ended with dangling tool_use",
                }}
                return
            if state is ResumeState.MISSING_COMPLETE_STEP:
                yield {"type": "error", "payload": {
                    "category": "agent_output_missing",
                    "kind": "missing_complete_step",
                    "message": "autonomous agent transcript ended without complete_step",
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

            assistant_msg, block_warnings, thinkings = _anthropic_to_message(response)
            for content in thinkings:
                yield {"type": "thinking", "payload": {"content": content}}
            for w in block_warnings:
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
                yield {"type": "error", "payload": {
                    "category": "agent_output_missing",
                    "kind": "no_complete_step",
                    "message": "agent finished without calling complete_step",
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
