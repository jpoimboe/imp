"""Drive ONE agent invocation as an attended, in-process terminal conversation.

Companion to detached.py: same build_agent_invocation/drive_invocation core, but
attended (interactive=True, headless=False) with a live renderer, a Ctrl-C
coordinator, and a setup-scoped slash-command interceptor. No quest, daemon, or
worktree — a one-time foreground setup task."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from imp.agents.skills import list_user_skills, load_user_skill
from imp.agents.transcript import SkillReply
from imp.cli.slash import (
    _COMMAND_SHAPED,
    REPROMPT,
    SANDBOX_STATE_OFF,
    SANDBOX_STATE_ON,
    is_slash_command,
)

_LIFECYCLE_COMMANDS = frozenset(
    {"save", "abandon", "shelve", "clear", "compact", "model"})

_SETUP_HELP_LINES = (
    "Setup commands:",
    "  /help     — show this list",
    "  /skills   — list available skills",
    "  /sandbox  — show sandbox status",
    "Other slash commands (e.g. /save, /abandon) are not available during setup.",
    "Type your answer and press Enter to continue.",
)


def handle_setup_slash(
    line: str,
    *,
    workflow_dir: Path,
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
        skills = list_user_skills(workflow_dir / "skills")
        if not skills:
            output_fn("No skills available.")
        for name, description in skills:
            output_fn(f"/{name}" + (f" — {description}" if description else ""))
        return REPROMPT
    if cmd == "sandbox":
        output_fn(SANDBOX_STATE_OFF if unsandboxed else SANDBOX_STATE_ON)
        return REPROMPT
    if cmd in _LIFECYCLE_COMMANDS:
        output_fn(f"/{cmd} is not available during setup.")
        return REPROMPT
    known = {name for name, _ in list_user_skills(workflow_dir / "skills")}
    if cmd in known:
        body = load_user_skill(workflow_dir / "skills", cmd)
        body = f"{body.rstrip()}\n\n{args}\n" if args else body
        return SkillReply(command=f"/{cmd}", name=cmd, body=body)
    if _COMMAND_SHAPED.match(cmd):
        output_fn(f"Unknown command: /{cmd} — /help for the list")
        return REPROMPT
    # Path-shaped (first token holds a `/` or `.`): submit verbatim.
    return line


def build_setup_input_fn(
    *,
    read_line: Callable[[str], Awaitable[str]],
    workflow_dir: Path,
    output_fn: Callable[[str], None],
    unsandboxed: bool,
) -> Callable[[str], Awaitable[str | SkillReply]]:
    """Return the user_chat_input_fn for the setup conversation.

    Reads one terminal line via the injected read_line seam, transparently
    handling slash commands, and returns the first real user line or loaded skill.
    Called as fn("") on every bare turn-end."""

    async def _input(_prompt: str) -> str | SkillReply:
        while True:
            line = await read_line("")
            if not is_slash_command(line):
                return line
            result = handle_setup_slash(
                line, workflow_dir=workflow_dir,
                output_fn=output_fn, unsandboxed=unsandboxed)
            if result is REPROMPT:
                continue
            assert isinstance(result, (str, SkillReply))
            return result

    return _input
