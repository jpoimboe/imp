"""Slash command dispatcher for interactive steps (spec §4).

Flat namespace: built-ins (/help, /skills) take precedence; every other
/<name> resolves a skill from the Change's tracked workflow dir and is submitted AS the
user's input line (human-attributed). The skill name is untrusted and
sanitized by agents.skills."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from imp.agents.skills import (
    SkillError,
    list_skill_names,
    load_skill_body,
)

# Sentinel: the line was a builtin / unknown / not-found command that printed
# and the caller should re-prompt (do NOT submit anything to the agent).
REPROMPT = object()


def is_slash_command(text: str) -> bool:
    return text.lstrip().startswith("/")


def handle_slash_line(
    text: str, *, change: Any, output_fn: Callable[[str], None],
) -> object | str:
    """Dispatch a /-line. Returns REPROMPT for /help, /skills, unknown, bad, or
    not-found commands (each prints via output_fn). Returns the skill body (str)
    for a successful /<name>, emitting a human-attributed skill_load event.

    `/<name> <args>` loads skill <name> and appends the trailing argument text
    after the body (blank-line separated), so `/refactor src/foo.py` submits the
    refactor skill followed by `src/foo.py`. The argument rides the returned user
    message; the attribution event stays {name, source}."""
    tokens = text.lstrip().lstrip("/").split(None, 1)
    cmd = tokens[0] if tokens else ""
    args = tokens[1].strip() if len(tokens) > 1 else ""
    skills_dir = change.workflow_dir / "skills"

    if cmd in ("", "help"):
        _help(output_fn)
        return REPROMPT
    if cmd == "skills":
        _list(skills_dir, output_fn)
        return REPROMPT

    try:
        body = load_skill_body(skills_dir, cmd)
    except SkillError:
        output_fn(f"no such skill: /{cmd}")
        _help(output_fn)
        return REPROMPT

    change.event_log().append(
        agent_id=None, type="skill_load",
        payload={"name": cmd, "source": "human"},
    )
    return f"{body.rstrip()}\n\n{args}\n" if args else body


def _list(skills_dir: Any, out: Callable[[str], None]) -> None:
    names = list_skill_names(skills_dir)
    if not names:
        out("No skills available in this workflow.")
        return
    out("Available skills (load with /<name>):")
    for n in names:
        out(f"  /{n}")


def _help(out: Callable[[str], None]) -> None:
    out("Available slash commands:")
    out("  /help              Show this message")
    out("  /skills            List loadable skills")
    out("  /<name> [args]     Load a skill (trailing args appended to it)")
    out("")
    out("Keybindings:")
    out("  Ctrl+C / Ctrl+D    Pause and exit; resume with `imp resume <id>`")
    out("  Up-arrow           Recall previous input")
    out("  Tab                Complete a /command or skill name")


async def ask_line_with_slash(
    prompt: Any, status: Any, change: Any, *, color: bool = False, width: int = 80,
) -> str:
    """Read one interactive line, intercepting slash commands. /help, /skills,
    unknown, and not-found commands print and re-prompt; /<name> returns the
    resolved skill body; anything else returns as-is.

    Framing (spec §3): a gray rule ABOVE the field (separates it from prior
    output) and, on a real submit, a matching rule BELOW — bracketing the
    user's turn so the input field reads as input, not agent output. The rules
    replace the old bright-blue background. Re-prompts (/help, /skills, unknown)
    and Ctrl-C/Ctrl-D exits draw no closing rule — only a submitted line does.

    Shared by the run and resume paths so the slash/skills logic exists once."""
    from imp.cli import palette as pal

    while True:
        # Keep the whole turn inside status.suspend(): /help, /skills, and
        # not-found messages print via handle_slash_line, and the live status
        # display must stay suspended while they do, or it interleaves/clobbers
        # them before the loop re-prompts.
        with status.suspend():
            print()                                    # breathing room above the rule
            print(pal.rule(width, enabled=color))      # §3: rule ABOVE the field
            line: str = await prompt.ask_async()
            if line.strip().lower() in ("exit", "quit"):
                # Equivalent to Ctrl-D: save-and-exit. Raise EOFError so the run
                # loop's existing EOFError handler (cli/main.py) pauses and exits.
                raise EOFError
            if not is_slash_command(line):
                print(pal.rule(width, enabled=color))  # rule BELOW closes the turn
                return line
            result = handle_slash_line(line, change=change, output_fn=print)
            if result is REPROMPT:
                continue
            assert isinstance(result, str)  # handle_slash_line returns str for skill body
            print(pal.rule(width, enabled=color))      # rule BELOW closes the turn
            return result
