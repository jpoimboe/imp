"""Slash command dispatcher for interactive steps (spec §4).

Flat namespace: built-ins (/help, /skills) take precedence; every other
/<name> resolves a skill from the Change's tracked workflow dir and is submitted AS the
user's input line (human-attributed). The skill name is untrusted and
sanitized by agents.skills."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, Callable
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
    if cmd == "continue":
        # /continue is the §2 resume verb at the post-interrupt prompt. The
        # session loop (cli/main) intercepts `/continue` BEFORE this dispatcher
        # to map it to slash="continue" and resume the paused run. If it reaches
        # here (typed at an ordinary wait_for_user solicit, where there is no run
        # to resume), it is a no-op that re-prompts rather than a skill lookup.
        output_fn("/continue resumes a paused run; there is nothing to resume here.")
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
    out("  /continue          Resume a paused run (at the post-interrupt prompt)")
    out("  /<name> [args]     Load a skill (trailing args appended to it)")
    out("")
    out("Keybindings:")
    out("  Ctrl+C / Ctrl+D    Pause and exit; resume with `imp resume <id>`")
    out("  Up-arrow           Recall previous input")
    out("  Tab                Complete a /command or skill name")


async def _ask_or_exit(prompt: Any, exit_evt: Any) -> str:
    """Read a line, but cancel the prompt if the coordinator exit event fires
    (SIGTERM / out-of-band SIGINT / escalation). On cancellation, raise
    KeyboardInterrupt so the CLI's pause-and-exit arm finalizes status=paused.

    The cancellation originates in THIS function's own inner task group, which
    cancels itself; by the time anything downstream (e.g. a wait_for_user
    solicit) observes a cancellation, it is the prompt's LOCAL scope unwinding,
    not an ancestor scope. We do NOT swallow CancelledError here — we let the
    scope unwind cleanly and synthesize KeyboardInterrupt only when the read
    never produced a line.

    The prompt's OWN exceptions (EOFError on Ctrl-D, KeyboardInterrupt on an
    out-of-band SIGINT) are raised inside `_read`, so anyio wraps them in a
    BaseExceptionGroup leaving the task group. We unwrap a lone such exception
    and re-raise it BARE, so the caller's `except EOFError`/`except
    KeyboardInterrupt` arms keep working (they'd miss a group)."""
    import anyio

    result: dict[str, str] = {}
    try:
        async with anyio.create_task_group() as tg:
            async def _watch() -> None:
                await exit_evt.wait()
                tg.cancel_scope.cancel()

            async def _read() -> None:
                result["line"] = await prompt.ask_async()
                tg.cancel_scope.cancel()

            tg.start_soon(_watch)
            tg.start_soon(_read)
    except BaseExceptionGroup as eg:
        if len(eg.exceptions) == 1:
            raise eg.exceptions[0] from None  # bare EOFError / KeyboardInterrupt
        raise
    if "line" not in result:
        raise KeyboardInterrupt  # exit-event won the race
    return result["line"]


async def ask_line_with_slash(
    prompt: Any, status: Any, change: Any, *, color: bool = False, width: int = 80,
    coord: Any = None,
) -> str:
    """Read one interactive line, intercepting slash commands. /help, /skills,
    unknown, and not-found commands print and re-prompt; /<name> returns the
    resolved skill body; anything else returns as-is.

    Framing: no chrome around the field. While the user types, only the
    bright-blue caret marks the turn; on submit prompt_toolkit erases its region
    (erase_when_done) and the live driver reprints the entered line with the
    subtle "entered input" background. The gray bracketing rules are gone — the
    background is the marker now.

    `coord` (the run's InterruptCoordinator) wraps the prompt turn in
    `coord.tty_quiesced()` so the ESC reader releases /dev/tty for
    prompt_toolkit; None (tests / non-interactive) = no-op.

    Shared by the run and resume paths so the slash/skills logic exists once."""

    @contextlib.asynccontextmanager
    async def _quiesce() -> AsyncGenerator[None, None]:
        if coord is None:
            yield
            return
        async with coord.tty_quiesced():
            yield

    # Quiesce for the WHOLE turn (every reprompt), not just one ask_async — so a
    # slash-command reprompt keeps the reader parked instead of briefly re-arming
    # cbreak mid-interaction (AC #3). Normal input returns immediately anyway.
    #
    # RE-ARM the coordinator's SIGINT/SIGTERM handlers on exit: prompt_toolkit's
    # prompt_async(handle_sigint=True) calls loop.remove_signal_handler(SIGINT)
    # when the prompt finishes, silently deleting the loop's SIGINT dispatch
    # entry. Without this re-arm the first prompt permanently disables mid-run
    # Ctrl-C, so a later interrupt is never seen by InterruptCoordinator (a
    # process-group Ctrl-C then surfaces as a codex `unknown` error). Runs on
    # every return/exception path, including the at-prompt KeyboardInterrupt.
    # Mark "at an interactive prompt" for the whole turn so an out-of-band SIGINT
    # (handle_sigint=False on the prompt → lands in InterruptCoordinator's loop
    # handler, NOT as a prompt_toolkit KBI) is routed through the coordinator exit
    # path instead of signal_pause/runner-interrupt. Reset in the finally on every
    # exit path (return, EOFError on Ctrl-D, KeyboardInterrupt, cancellation).
    if coord is not None:
        coord.set_at_prompt(True)
    try:
        async with _quiesce():
            while True:
                # Keep the whole turn inside status.suspend(): /help, /skills, and
                # not-found messages print via handle_slash_line, and the live status
                # display must stay suspended while they do, or it interleaves/clobbers
                # them before the loop re-prompts.
                with status.suspend():
                    # _exit_event is None until C4 wires it; getattr is defensive
                    # for pre-C4 builds, where the read takes the unraced path.
                    exit_evt = getattr(coord, "_exit_event", None) if coord else None
                    try:
                        if exit_evt is None:
                            line: str = await prompt.ask_async()
                        else:
                            line = await _ask_or_exit(prompt, exit_evt)
                    except KeyboardInterrupt:
                        # A KeyboardInterrupt reaching here is normally the
                        # exit-event-won synthesis from _ask_or_exit: an out-of-band
                        # SIGINT at the prompt now lands in the coordinator's loop
                        # handler (handle_sigint=False), which (via _at_prompt) calls
                        # request_exit("ctrl-c") → fires the exit event → _ask_or_exit
                        # cancels the read and raises KeyboardInterrupt. The c-c key
                        # binding handles TYPED Ctrl-C (no KBI). request_exit here is a
                        # defensive idempotent re-assert (first-wins pause_reason keeps
                        # "ctrl-c") covering any path that raises a bare KBI; we then
                        # re-raise so the CLI's except-KeyboardInterrupt arm runs
                        # pause_and_exit. The finally still resets _at_prompt.
                        if coord is not None:
                            coord.request_exit("ctrl-c")
                        raise
                    if line.strip().lower() in ("exit", "quit"):
                        # Equivalent to Ctrl-D: save-and-exit. Raise EOFError so the
                        # run loop's existing EOFError handler (cli/main.py) pauses.
                        raise EOFError
                    if not is_slash_command(line):
                        return line
                    result = handle_slash_line(line, change=change, output_fn=print)
                    if result is REPROMPT:
                        continue
                    assert isinstance(result, str)  # handle_slash_line returns str (skill body)
                    return result
    finally:
        # Leave the at-prompt state so a SIGINT during a subsequent RUN takes the
        # signal_pause path again (the headline mid-run Ctrl-C). Runs on every
        # return/exception/cancellation path.
        if coord is not None:
            coord.set_at_prompt(False)
            # With handle_sigint=False prompt_toolkit no longer deletes the loop's
            # SIGINT handler on prompt exit, so this is now a harmless defensive
            # no-op — kept so a future provider that re-enables handle_sigint, or a
            # nested prompt that does, still re-arms (see ask_async rationale).
            coord.rearm_signal_handlers()
