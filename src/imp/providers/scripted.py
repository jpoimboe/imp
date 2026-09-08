# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Scripted provider: a deterministic, offline AgentRunner for tests and CI."""

from __future__ import annotations

import contextlib
import inspect
import json
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast

from imp.agents.dispatch import TurnState, dispatch_turn_tool_uses
from imp.agents.runner import (
    AgentInvocation,
    AgentRunner,
    Event,
    Message,
    TextBlock,
    ToolUseBlock,
)
from imp.agents.tools import Tool, get_registry
from imp.agents.tools.complete_step import make_complete_step_tool
from imp.agents.tools.context import ToolContext
from imp.agents.transcript import Transcript, deliver_continuation_reply
from imp.engine.agent_run import last_assistant_text
from imp.providers.base import Provider
from imp.providers.resolve import spawn_subagent_via_registry

ENV_VAR = "IMP_SCRIPTED_SCRIPT"


def _env_check() -> bool:
    return bool(os.environ.get(ENV_VAR))


def _load_script() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(Path(os.environ[ENV_VAR]).read_text()))


def _select_turns(script: dict[str, Any], agent_name: str) -> list[dict[str, Any]]:
    steps = script.get("steps") or {}
    return steps.get(agent_name) or script.get("default") or []


def _build_tools(invocation: AgentInvocation) -> dict[str, Tool]:
    reg = get_registry()
    out: dict[str, Tool] = {}
    for name in invocation.agent_def.tools:
        if name in ("complete_step", "sub_agent", "load_skill"):
            continue
        if name in reg:
            out[name] = reg[name]
    if invocation.subagent_depth == 0:
        out["complete_step"] = make_complete_step_tool(
            invocation.agent_def.output_schema)
    if "bash" in invocation.agent_def.tools:
        out["bash_output"] = reg["bash_output"]
        out["bash_kill"] = reg["bash_kill"]
    return out


class ScriptedAgentRunner:
    def __init__(self, model_name: str) -> None:
        self.model = model_name
        self._ctx: ToolContext | None = None

    async def run(
        self, invocation: AgentInvocation,
    ) -> AsyncGenerator[Event, None]:
        from imp.agents.tools.context import teardown_tool_context
        from imp.engine.state import JsonlEventLog

        elog = invocation.event_log or JsonlEventLog(invocation.events_path)
        aid = invocation.agent_id or "step:unnamed"

        transcript = Transcript(invocation.transcript_dir / "transcript.jsonl")
        tools = _build_tools(invocation)
        ctx = ToolContext(
            project_root=invocation.quest_context.project_root,
            events_path=invocation.events_path,
            subagent_defs=invocation.subagent_defs,
            provider_name=invocation.provider_name,
            model_name=invocation.model_name,
            parent_transcript_dir=invocation.transcript_dir,
            agent_id=aid,
            background_enabled=True,
            quest_home=invocation.quest_context.quest_dir,
        )
        ctx.quest_context = invocation.quest_context
        ctx.quest_state = invocation.quest_state
        ctx.quest = invocation.quest
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
        ctx.user_input_fn = invocation.user_input_fn
        ctx.event_log = elog
        self._ctx = ctx

        try:
            messages: list[Message] = []
            for m in invocation.initial_messages:
                messages.append(m)
                transcript.append(m)

            elog.append_step_context(
                agent_id=aid, parent_id_override=invocation.first_event_parent_id,
                provider=invocation.provider_name, model=self.model,
                tools_schema=[{"name": t.name} for t in tools.values()],
                params={}, max_tool_calls=invocation.max_tool_calls)

            elog.append_engine_message(
                agent_id=aid,
                message=Message(role="system",
                                content=(TextBlock(text=invocation.system_prompt),)),
                subtype="system_prompt")
            for m in invocation.initial_messages:
                if all(isinstance(b, TextBlock) for b in m.content):
                    elog.append_engine_message(
                        agent_id=aid, message=m, subtype="agent_prompt")

            ceiling = invocation.max_tool_calls
            turns = _select_turns(_load_script(), invocation.agent_def.name)
            used = 0

            for ti, turn in enumerate(turns):
                blocks: list[Any] = []
                text = turn.get("text", "")
                if text:
                    blocks.append(TextBlock(text=text))
                tool_uses: list[ToolUseBlock] = []
                for ci, call in enumerate(turn.get("tools", [])):
                    tu = ToolUseBlock(
                        id=f"{aid}:{ti}:{ci}",
                        name=call["name"],
                        input=call.get("input", {}),
                    )
                    tool_uses.append(tu)
                    blocks.append(tu)

                assistant = Message(role="assistant", content=tuple(blocks))
                messages.append(assistant)
                transcript.append(assistant)
                elog.append_agent_message(
                    agent_id=aid, provider=invocation.provider_name,
                    model=self.model, raw={"content": []}, usage={},
                    stop_reason=None)

                tool_results: list[Any] = []
                state = TurnState(tool_calls_used=used)
                # Shared turn partition (same as the real providers): sub_agent
                # tus spawn children via ctx.spawn_subagent; everything else
                # flows through dispatch_one_tool_use inside.
                async for ev in dispatch_turn_tool_uses(
                        ctx=ctx, elog=elog, aid=aid, invocation=invocation,
                        tools=tools, tool_uses=tool_uses,
                        tool_results=tool_results, state=state,
                        ceiling=ceiling):
                    yield ev
                    if ev.get("type") != "tool_heartbeat":
                        return
                used = state.tool_calls_used

                if tool_results:
                    user_msg = Message(role="user", content=tuple(tool_results))
                    messages.append(user_msg)
                    transcript.append(user_msg)

                if not tool_uses and invocation.subagent_depth > 0:
                    # Subagent bare turn-end is terminal SUCCESS: its final text
                    # is the deliverable. Empty text walks back; none -> emit
                    # nothing so drive_invocation surfaces an errored result.
                    final_text = last_assistant_text(messages)
                    if final_text:
                        yield {"type": "agent_result",
                               "payload": {"text": final_text}}
                    return

                if not tool_uses and invocation.continuation_fn is not None:
                    # Bare turn-end (assistant text, no tool call) IS the yield:
                    # route through the shared end-of-turn handler like the real
                    # providers (solicit / nudge / pause), then continue the script.
                    nxt, human = invocation.continuation_fn(text)
                    if inspect.isawaitable(nxt):
                        nxt = await nxt
                    deliver_continuation_reply(
                        messages, transcript, elog, aid, nxt, human=human)

            yield {"type": "error", "payload": {
                "category": "agent_output_missing",
                "kind": "no_complete_step",
                "message": "scripted runner exhausted script without complete_step",
            }}
        finally:
            self._ctx = None
            with contextlib.suppress(Exception):
                await teardown_tool_context(ctx)

    async def interrupt(self) -> None:
        from imp.agents.tools.context import terminate_tool_processes
        ctx = self._ctx
        if ctx is not None:
            with contextlib.suppress(Exception):
                await terminate_tool_processes(ctx)


class ScriptedProvider:
    name = "scripted"

    def __init__(self, model_name: str) -> None:
        self.model = model_name

    def create_runner(self, invocation: AgentInvocation) -> AgentRunner:
        return ScriptedAgentRunner(model_name=self.model)


def _factory(model: str) -> Provider:
    return ScriptedProvider(model_name=model)
