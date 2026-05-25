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

Per review #2: deepagents 0.6.x has no excluded_tools kwarg. Built-in tools
(read_file, write_file, execute, write_todos, task, ask_user, etc.) ship
to every agent regardless of agent.md `tools:` declaration. We only filter
the imp-provided tools (query_events, web_fetch, git_diff) at source.
Built-in tool exclusion is a Plan 3 follow-up.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from pydantic import BaseModel

from imp.agents.backends import build_backend
from imp.agents.chat_middleware import ChatMiddleware
from imp.agents.output_schema import schema_to_pydantic_model
from imp.agents.runner import AgentInvocation
from imp.agents.subagents import compile_subagent_specs
from imp.agents.tools import (
    build_git_diff_tool, build_query_events_tool, build_web_fetch_tool,
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
        text = msg.content if isinstance(msg.content, str) else ""
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
            try:
                suppress_initial.remove(text)
            except ValueError:
                pass
            return out
        out.append({"type": "user_input", "payload": {"text": text}})
    return out


class DeepAgentRunner:
    def __init__(self, model_factory: Any = None) -> None:
        """model_factory is a test escape hatch (review minor): if set,
        called as model_factory(invocation) to override invocation.model."""
        self._model_factory = model_factory

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
        backend = build_backend(invocation)
        subagent_specs = compile_subagent_specs(
            invocation.agent_def,
            agents_dir=invocation.agent_def.source_path.parent,
            model=model,
        )

        middleware: list[Any] = []
        if invocation.interactive:
            middleware.append(ChatMiddleware(input_fn=input))

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

        structured: BaseModel | None = None
        try:
            for chunk in agent.stream(
                {"messages": initial_messages},
                stream_mode="updates",
            ):
                for msg in _extract_messages(chunk):
                    for event in _translate_message(msg, suppress):
                        yield event
                sr = _extract_structured_response(chunk)
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
