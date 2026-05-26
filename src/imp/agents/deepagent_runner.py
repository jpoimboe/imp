"""DeepAgentRunner: builds a deepagent from an AgentInvocation, streams events,
translates them to imp wire format, yields. Terminal event derives from the
final `structured_response` field of the agent's result.

Design pivot (review #5): instead of a complete_step tool whose body raises,
we set response_format=schema_to_pydantic_model(agent_def.output_schema) on
create_deep_agent. The LLM's native structured output mechanism produces the
terminal response; we read it from the last chunk of agent.stream.

Per Spike 2: result['structured_response'] holds the Pydantic instance;
in streaming, the structured_response surfaces under the chunk's 'model' key.

Per Spike 4 + Task 13: backend is a CompositeBackend routing /artifacts,
/project to disk via FilesystemBackend(virtual_mode=True); / falls back
to StateBackend.

Per review #2 / Task 4: deepagents 0.6.x has no excluded_tools kwarg. Built-in
tools (read_file, write_file, execute, write_todos, task, etc.) ship to every
agent regardless of agent.md `tools:` declaration. Declaration-based gating is
implemented via stub shadowing in builtin_filter.build_filtered_builtin_overrides.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import Any

from deepagents import create_deep_agent
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from pydantic import BaseModel

from imp.agents.backends import build_backend
from imp.agents.builtin_filter import build_filtered_builtin_overrides
from imp.agents.chat_middleware import ChatMiddleware
from imp.agents.output_schema import schema_to_pydantic_model
from imp.agents.runner import AgentInvocation
from imp.agents.subagents import compile_subagent_specs
from imp.agents.tools import (
    build_git_diff_tool,
    build_query_events_tool,
    build_web_fetch_tool,
)


def _build_imp_tools(invocation: AgentInvocation) -> list[Any]:
    """Build only the imp tools the agent declares.

    Built-in deepagent tools (read_file, write_file, execute, etc.) are NOT
    filterable in 0.6.x — they ship to every agent. Plan 3 follow-up.
    """
    declared = invocation.agent_def.tools
    tools: list[Any] = []
    if "query_events" in declared and invocation.events_path is not None:
        tools.append(build_query_events_tool(invocation.events_path))
    if "web_fetch" in declared:
        tools.append(build_web_fetch_tool())
    if "git_diff" in declared:
        tools.append(build_git_diff_tool(invocation.project_root))
    return tools


def _extract_messages(chunk: dict[str, Any]) -> list[BaseMessage]:
    """deepagents stream chunks are middleware-qualified maps. Walk every
    value and pick up any `messages` list, regardless of key (per Spike 2)."""
    out: list[BaseMessage] = []

    def _walk(v: Any) -> None:
        if isinstance(v, dict):
            msgs = v.get("messages")
            if isinstance(msgs, list):
                for m in msgs:
                    if isinstance(m, BaseMessage):
                        out.append(m)
            for vv in v.values():
                if isinstance(vv, dict):
                    _walk(vv)
        elif isinstance(v, list):
            for item in v:
                _walk(item)

    _walk(chunk)
    return out


def _extract_structured_response(chunk: dict[str, Any]) -> BaseModel | None:
    """Walk chunk looking for structured_response (typically under 'model' key
    per Spike 2)."""

    def _walk(v: Any) -> BaseModel | None:
        if isinstance(v, dict):
            sr = v.get("structured_response")
            if isinstance(sr, BaseModel):
                return sr
            for vv in v.values():
                r = _walk(vv)
                if r is not None:
                    return r
        return None

    return _walk(chunk)


def _extract_text(content: Any) -> str:
    """Pull plain text out of a BaseMessage.content value.

    Anthropic content is a list of blocks like
    [{"type": "text", "text": "..."}, {"type": "tool_use", ...}].
    OpenAI typically returns a plain str. Streamed AIMessageChunks from
    Anthropic also use the list-of-blocks shape mid-token.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    t = block.get("text", "")
                    if isinstance(t, str):
                        parts.append(t)
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def _translate_message(
    msg: BaseMessage, suppress_initial: list[str],
) -> list[dict[str, Any]]:
    """Translate one BaseMessage to 0+ imp events."""
    out: list[dict[str, Any]] = []
    if isinstance(msg, AIMessage):
        for tc in msg.tool_calls or []:
            out.append({
                "type": "tool_call",
                "payload": {
                    "id": tc.get("id", ""),
                    "name": tc.get("name", ""),
                    "args": tc.get("args", {}),
                },
            })
        text = _extract_text(msg.content)
        if text:
            out.append({"type": "agent_message", "payload": {"text": text}})
    elif isinstance(msg, ToolMessage):
        result = msg.content if isinstance(msg.content, str) else str(msg.content)
        out.append({
            "type": "tool_result",
            "payload": {
                "tool_call_id": getattr(msg, "tool_call_id", ""),
                "result": result,
            },
        })
    elif isinstance(msg, HumanMessage):
        text = msg.content if isinstance(msg.content, str) else ""
        # Suppress bootstrap messages we injected ourselves (review minor)
        if text in suppress_initial:
            with contextlib.suppress(ValueError):
                suppress_initial.remove(text)
            return out
        out.append({"type": "user_input", "payload": {"text": text}})
    return out


def _process_stream_chunk(
    mode: str, payload: Any, suppress: list[str],
) -> Iterator[dict[str, Any]]:
    """Translate one stream chunk (from dual-mode stream) to imp events.

    mode='messages': payload is (AIMessageChunk, metadata) — emit agent_token
    mode='updates': payload is a node-update dict — emit translated messages
        (agent_message, tool_call, tool_result, user_input) and extract
        structured_response if present.
    """
    if mode == "messages":
        chunk, _meta = payload if isinstance(payload, tuple) else (payload, {})
        if isinstance(chunk, AIMessageChunk):
            # Anthropic streams content as list-of-dicts blocks even mid-token;
            # extract any text fragment from either str or list[dict] shape.
            text = _extract_text(chunk.content)
            if text:
                yield {"type": "agent_token", "payload": {"text": text}}
        return
    if mode == "updates" and isinstance(payload, dict):
        for msg in _extract_messages(payload):
            yield from _translate_message(msg, suppress)
        return


class DeepAgentRunner:
    def __init__(
        self, model_factory: Any = None,
        *, input_fn: Callable[[], str] | None = None,
    ) -> None:
        """model_factory is a test escape hatch (review minor): if set,
        called as model_factory(invocation) to override invocation.model."""
        self._model_factory = model_factory
        self._input_fn = input_fn or input

    def run(self, invocation: AgentInvocation) -> Iterator[dict[str, Any]]:
        model = (
            self._model_factory(invocation)
            if self._model_factory
            else invocation.model
        )
        if model is None:
            yield {"type": "error", "payload": {"message": "no model resolved"}}
            return

        response_format = schema_to_pydantic_model(
            invocation.agent_def.output_schema
        )
        tools = _build_imp_tools(invocation)
        tools.extend(build_filtered_builtin_overrides(
            invocation.agent_def.tools,
        ))
        backend = build_backend(invocation)
        subagent_specs = compile_subagent_specs(
            invocation.agent_def,
            agents_dir=invocation.agent_def.source_path.parent,
            model=model,
        )

        middleware: list[Any] = []
        if invocation.interactive:
            middleware.append(ChatMiddleware(input_fn=self._input_fn))

        kwargs: dict[str, Any] = {
            "model": model,
            "tools": tools,
            "system_prompt": invocation.system_prompt
            or invocation.agent_def.prompt_template_source,
            "backend": backend,
            "response_format": response_format,
        }
        if middleware:
            kwargs["middleware"] = middleware
        if subagent_specs:
            kwargs["subagents"] = subagent_specs
        if invocation.checkpointer is not None:
            kwargs["checkpointer"] = invocation.checkpointer

        try:
            agent = create_deep_agent(**kwargs)
        except Exception as e:
            yield {"type": "error", "payload": {
                "message": f"create_deep_agent failed: {type(e).__name__}: {e}",
            }}
            return

        initial_messages = (
            list(invocation.initial_messages)
            or [{"role": "user", "content": "begin"}]
        )
        suppress: list[str] = [
            m["content"] for m in initial_messages
            if isinstance(m, dict) and isinstance(m.get("content"), str)
        ]

        # langgraph requires a `thread_id` in the config whenever a
        # checkpointer is attached. We derive a per-step thread id from
        # change_id + agent name so each step gets its own checkpoint scope.
        stream_kwargs: dict[str, Any] = {"stream_mode": ["updates", "messages"]}
        if invocation.checkpointer is not None:
            stream_kwargs["config"] = {
                "configurable": {
                    "thread_id": f"change-{invocation.change_id}-"
                                 f"{invocation.agent_def.name}",
                },
            }

        structured: BaseModel | None = None
        try:
            for item in agent.stream(  # type: ignore[call-overload]
                {"messages": initial_messages},
                **stream_kwargs,
            ):
                # In multi-mode the iterator yields (mode, payload) tuples
                if isinstance(item, tuple) and len(item) == 2:
                    mode, payload = item
                else:
                    mode, payload = "updates", item
                yield from _process_stream_chunk(mode, payload, suppress)
                if mode == "updates" and isinstance(payload, dict):
                    sr = _extract_structured_response(payload)
                    if sr is not None:
                        structured = sr
        except Exception as e:
            yield {"type": "error", "payload": {
                "message": f"runner crashed: {type(e).__name__}: {e}",
            }}
            return

        if structured is None:
            yield {"type": "error", "payload": {
                "message": "agent finished without producing structured_response",
            }}
            return

        payload = structured.model_dump(exclude_none=True)
        yield {"type": "agent_output", "payload": payload}
