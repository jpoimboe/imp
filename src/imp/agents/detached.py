# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Drive ONE agent invocation to completion outside run_quest/the executor."""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio

from imp.agents.prompts import build_context, render_prompt
from imp.agents.runner import QuestContext
from imp.engine.agent_run import AgentRunResult, build_agent_invocation, drive_invocation
from imp.engine.env import engine_env, overlay_engine_env
from imp.engine.sandbox import (
    SandboxConfig,
    SandboxUnavailable,
    resolve_policy,
    sandbox_available,
)
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef
from imp.providers.config import ProviderModel
from imp.providers.resolve import provider_from_model

_DETECT_QUEST_ID = "init-detect"


@dataclass(frozen=True, slots=True)
class DetachedSuccess:
    output: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DetachedEnvironmentFailure:
    category: str  # "sandbox" | "auth" | "config"
    detail: str


@dataclass(frozen=True, slots=True)
class DetachedTransientFailure:
    category: str  # "network" | "timeout" | "stalled" | "no_output" | ...
    detail: str


DetachedResult = (
    DetachedSuccess | DetachedEnvironmentFailure | DetachedTransientFailure
)


async def run_detached_agent(
    *,
    agent_def: AgentDef,
    provider_name: str,
    model_name: str,
    initial_message: str,
    project_root: Path,
    workflow_dir: Path,
    sandbox_cfg: SandboxConfig,
    title: str = "detached agent",
    tier: str = "medium",
    max_tool_calls: int = 25,
    timeout_s: float = 60.0,
) -> DetachedResult:
    eng = overlay_engine_env(
        os.environ,
        engine_env(project_root=project_root, quest_id=_DETECT_QUEST_ID,
                   quest_title=title, control_root=project_root),
    )
    try:
        policy = resolve_policy(
            worktree=project_root, control_root=project_root,
            quest_id=_DETECT_QUEST_ID, quest_dir=None, cfg=sandbox_cfg,
            engine_env_result=eng, environ=os.environ,
            available=sandbox_available(),
        )
    except SandboxUnavailable as e:
        return DetachedEnvironmentFailure("sandbox", str(e))

    rendered = render_prompt(
        agent_def.prompt_template_source,
        build_context(quest_title=title, event_log=InMemoryEventLog()),
        workflow_dir=workflow_dir,
    )
    rendered_with_kickoff = (
        f"{rendered}\n\n{initial_message}" if initial_message else rendered
    )
    provider = provider_from_model(ProviderModel(provider_name, model_name))
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        invocation = build_agent_invocation(
            agent_def=agent_def,
            provider_name=provider.name,
            model_name=provider.model,
            rendered_system_prompt=rendered_with_kickoff,
            initial_messages=(),
            quest_context=QuestContext(
                id=_DETECT_QUEST_ID, title=title, project_root=project_root,
                workflow_dir=workflow_dir),
            user_chat_input_fn=None,
            transcript_dir=tmp / "transcript",
            events_path=tmp / "events.jsonl",
            subagent_defs={},
            tiers=(),
            max_tool_calls=max_tool_calls,
            agent_id="detached",
            event_log=None,
            quest_state=None,
            quest=None,
            first_event_parent_id=None,
            resolved_tiers=None,
            coordinator=None,
            tier=tier,
            subagent_depth=0,
            headless=True,
            interactive=False,
            sandbox_policy=policy,
        )
        runner = provider.create_runner(invocation)
        try:
            with anyio.fail_after(timeout_s):
                result = await drive_invocation(
                    invocation, runner, coordinator=None, log=None,
                    emit_events=False)
        except TimeoutError:
            return DetachedTransientFailure(
                "timeout", f"agent run exceeded {timeout_s:g}s")

    return _classify(result)


def _classify(result: AgentRunResult) -> DetachedResult:
    if result.output is not None and not result.errored and not result.paused:
        return DetachedSuccess(result.output)
    err = result.error
    if err is not None and err.category in ("auth", "config"):
        return DetachedEnvironmentFailure(err.category, err.kind or "")
    if err is not None:
        return DetachedTransientFailure(err.category, err.kind or "")
    return DetachedTransientFailure("no_output", "agent produced no output")
