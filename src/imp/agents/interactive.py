"""Drive ONE agent invocation as an attended, in-process terminal conversation.

Companion to detached.py: same build_agent_invocation/drive_invocation core, but
attended (interactive=True, headless=False) with a live renderer, a Ctrl-C
coordinator, and a setup-scoped slash-command interceptor. No quest, daemon, or
worktree — a one-time foreground setup task."""
from __future__ import annotations

import functools
import os
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio

from imp.agents.prompts import build_context, render_prompt
from imp.agents.runner import QuestContext
from imp.agents.skills import list_user_skills, load_user_skill
from imp.agents.transcript import SkillReply
from imp.cli.live_driver import LiveDriver
from imp.cli.live_status import LiveStatus
from imp.cli.signals import InterruptCoordinator
from imp.cli.slash import (
    _COMMAND_SHAPED,
    REPROMPT,
    solicit_line,
)
from imp.engine.agent_run import (
    AgentRunResult,
    build_agent_invocation,
    drive_invocation,
)
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

_LIFECYCLE_COMMANDS = frozenset(
    {"save", "abandon", "shelve", "clear", "compact", "model"})

_SETUP_QUEST_ID = "init-setup"

_SETUP_HELP_LINES = (
    "Setup commands:",
    "  /help     — show this list",
    "  /skills   — list available skills",
    "  /sandbox  — show sandbox status",
    "Other slash commands (e.g. /save, /abandon) are not available during setup.",
    "Type your answer and press Enter to continue.",
)

SANDBOX_SETUP_ON = (
    "Sandbox is on. Shell and file tools are confined to this project "
    "directory; secrets are masked.")
SANDBOX_SETUP_OFF = (
    "Sandbox is off — setup is running unsandboxed. Shell and file tools "
    "run as you: your home directory and secrets are readable and writes "
    "are unconfined.")


@dataclass(frozen=True, slots=True)
class InteractiveSuccess:
    output: dict[str, Any]


@dataclass(frozen=True, slots=True)
class InteractiveFailure:
    category: str  # "sandbox" | "auth" | "config" | "network" | "no_output" | ...
    detail: str


InteractiveResult = InteractiveSuccess | InteractiveFailure


def handle_setup_slash(
    line: str,
    *,
    asset_dir: Path,
    output_fn: Callable[[str], None],
    unsandboxed: bool,
) -> object | str | SkillReply:
    """Handle one slash line typed during setup.

    Returns REPROMPT when handled locally (keep reading), a SkillReply when a
    user skill is loaded (inject as the agent's next message), or the raw line
    as a str when it is path-shaped (contains `/` or `.`) — submitted verbatim,
    matching the daemon handler's passthrough. Lifecycle commands print "not
    available during setup" rather than being sent to the agent."""
    tokens = line.lstrip().lstrip("/").split(None, 1)
    cmd = tokens[0] if tokens else ""
    args = tokens[1].strip() if len(tokens) > 1 else ""

    if cmd in ("", "help"):
        for text in _SETUP_HELP_LINES:
            output_fn(text)
        return REPROMPT
    if cmd == "skills":
        skills = list_user_skills(asset_dir / "skills")
        if not skills:
            output_fn("No skills available.")
        for name, description in skills:
            output_fn(f"/{name}" + (f" — {description}" if description else ""))
        return REPROMPT
    if cmd == "sandbox":
        output_fn(SANDBOX_SETUP_OFF if unsandboxed else SANDBOX_SETUP_ON)
        return REPROMPT
    if cmd in _LIFECYCLE_COMMANDS:
        output_fn(f"/{cmd} is not available during setup.")
        return REPROMPT
    known = {name for name, _ in list_user_skills(asset_dir / "skills")}
    if cmd in known:
        body = load_user_skill(asset_dir / "skills", cmd)
        body = f"{body.rstrip()}\n\n{args}\n" if args else body
        return SkillReply(command=f"/{cmd}", name=cmd, body=body)
    if _COMMAND_SHAPED.match(cmd):
        output_fn(f"Unknown command: /{cmd} — /help for the list")
        return REPROMPT
    # Path-shaped (first token holds a `/` or `.`): submit verbatim.
    return line


def build_setup_input_fn(
    *,
    prompt: Any,
    asset_dir: Path,
    output_fn: Callable[[str], None],
    unsandboxed: bool,
    status: Any,
    driver: Any,
    coord: Any,
    width: int = 80,
) -> Callable[[str], Awaitable[str | SkillReply]]:
    """Return the user_chat_input_fn for the setup conversation.

    One shared solicit turn per read (slash.solicit_line) with setup's own slash
    dispatch injected, so the field gets the run path's caret, bottom toolbar,
    and spacing. Called as fn("") on every bare turn-end."""

    def _dispatch(line: str) -> object:
        return handle_setup_slash(
            line, asset_dir=asset_dir, output_fn=output_fn,
            unsandboxed=unsandboxed)

    async def _input(_prompt: str) -> str | SkillReply:
        result = await solicit_line(
            prompt, status, dispatch=_dispatch,
            toolbar_fn=driver.compose_toolbar_static, coord=coord,
            driver=driver, width=width, output_fn=output_fn)
        assert isinstance(result, (str, SkillReply))
        return result

    return _input


async def run_interactive_agent(
    *,
    agent_def: AgentDef,
    provider_name: str,
    model_name: str,
    initial_message: str,
    project_root: Path,
    asset_dir: Path,
    sandbox_cfg: SandboxConfig,
    prompt: Any,
    stream: Any = None,
    output_fn: Callable[[str], None] | None = None,
    color: bool = False,
    width: int = 80,
    title: str = "imp setup",
    tier: str = "medium",
    max_tool_calls: int = 25,
) -> InteractiveResult:
    """Drive one agent invocation as an attended terminal conversation."""
    stream = sys.stdout if stream is None else stream
    if output_fn is None:
        def output_fn(message: str) -> None:
            print(message, file=stream)

    eng = overlay_engine_env(
        os.environ,
        engine_env(project_root=project_root, quest_id=_SETUP_QUEST_ID,
                   quest_title=title),
    )
    try:
        policy = resolve_policy(
            worktree=project_root, control_root=project_root,
            quest_id=_SETUP_QUEST_ID, quest_dir=None, cfg=sandbox_cfg,
            engine_env_result=eng, environ=os.environ,
            available=sandbox_available(),
        )
    except SandboxUnavailable as e:
        return InteractiveFailure("sandbox", str(e))
    unsandboxed = not policy.active

    rendered = render_prompt(
        agent_def.prompt_template_source,
        build_context(quest_title=title, event_log=InMemoryEventLog()),
        workflow_dir=asset_dir,
    )
    rendered_with_kickoff = (
        f"{rendered}\n\n{initial_message}" if initial_message else rendered
    )
    provider = provider_from_model(ProviderModel(provider_name, model_name))

    status = LiveStatus(stream)
    driver = LiveDriver(stream=stream, color=color, width=width, status=status,
                        title=title)
    driver.set_unsandboxed(unsandboxed)
    event_log = InMemoryEventLog(on_event=driver.on_event)
    coord = InterruptCoordinator()
    input_fn = build_setup_input_fn(
        prompt=prompt, asset_dir=asset_dir, output_fn=output_fn,
        unsandboxed=unsandboxed, status=status, driver=driver, coord=coord,
        width=width)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        invocation = build_agent_invocation(
            agent_def=agent_def,
            provider_name=provider.name,
            model_name=provider.model,
            rendered_system_prompt=rendered_with_kickoff,
            initial_messages=(),
            quest_context=QuestContext(
                id=_SETUP_QUEST_ID, title=title, project_root=project_root,
                workflow_dir=asset_dir),
            user_chat_input_fn=input_fn,
            transcript_dir=tmp / "transcript",
            events_path=tmp / "events.jsonl",
            subagent_defs={},
            tiers=(),
            max_tool_calls=max_tool_calls,
            agent_id="setup",
            event_log=event_log,
            quest_state=None,
            quest=None,
            first_event_parent_id=None,
            resolved_tiers=None,
            coordinator=coord,
            tier=tier,
            subagent_depth=0,
            headless=False,
            interactive=True,
            sandbox_policy=policy,
        )
        runner = provider.create_runner(invocation)
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(coord.run_signal_receiver)
                tg.start_soon(status.tick)
                driver.install_startup_status()
                try:
                    result = await drive_invocation(
                        invocation, runner, coordinator=coord, log=None,
                        emit_events=False)
                finally:
                    tg.cancel_scope.cancel()
        finally:
            driver.finish()

    return _classify(result)


def run_interactive_agent_sync(**kwargs: Any) -> InteractiveResult:
    """Synchronous entry: run the interactive host under its own event loop."""
    return anyio.run(functools.partial(run_interactive_agent, **kwargs))


def _classify(result: AgentRunResult) -> InteractiveResult:
    if result.output is not None and not result.errored and not result.paused:
        return InteractiveSuccess(result.output)
    err = result.error
    if err is not None:
        return InteractiveFailure(err.category, err.kind or "")
    return InteractiveFailure("no_output", "agent produced no output")
