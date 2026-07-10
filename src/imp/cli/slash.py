"""Slash command dispatcher for interactive steps (spec §4).

Flat namespace: built-ins (/help, /skills) take precedence; every other
/<name> resolves a skill from the Change's tracked workflow dir and is submitted AS the
user's input line (human-attributed). The skill name is untrusted and
sanitized by agents.skills."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from imp.agents.skills import (
    SkillError,
    list_user_skills,
    load_user_skill,
)
from imp.cli.live_driver import compose_identity_toolbar
from imp.cli.model_command import print_model_roster, validate_model_arg

# Sentinel: the line was a builtin / unknown / not-found command that printed
# and the caller should re-prompt (do NOT submit anything to the agent).
REPROMPT = object()


def is_slash_command(text: str) -> bool:
    return text.lstrip().startswith("/")


def handle_slash_line(
    text: str, *, change: Any, output_fn: Callable[[str], None],
) -> object | str:
    """Dispatch a /-line. Returns REPROMPT for /help, /skills, and builtins that
    print usage (each prints via output_fn). Returns the skill body (str) for a
    successful /<name>, emitting a human-attributed skill_load event. A /-line
    that matches no builtin and no skill is returned verbatim (str) and submitted
    as ordinary input — so a pasted path like `/home/user/foo.py` is not misread
    as a command.

    `/<name> <args>` loads skill <name> and appends the trailing argument text
    after the body (blank-line separated), so `/refactor src/foo.py` submits the
    refactor skill followed by `src/foo.py`. The argument rides the returned user
    message; the attribution event stays {name, source}."""
    tokens = text.lstrip().lstrip("/").split(None, 1)
    cmd = tokens[0] if tokens else ""
    args = tokens[1].strip() if len(tokens) > 1 else ""

    if cmd in ("", "help"):
        _help(output_fn)
        return REPROMPT
    if cmd == "skills":
        _list(change.workflow_dir / "skills", output_fn)
        return REPROMPT
    if cmd == "list":
        import argparse

        from imp.cli.main import _cmd_status
        show_all = args in ("-a", "--all", "all")
        _cmd_status(argparse.Namespace(all=show_all), change.control_root)
        return REPROMPT
    if cmd == "search":
        if not args:
            output_fn("Usage: /search <query>")
            return REPROMPT
        from imp.engine.archive import read as archive_read
        hits = archive_read.search_archive(change.control_root, args)
        if not hits:
            output_fn(f"No archived changes match {args!r}.")
        else:
            for quest_id, title, status, _created in hits:
                output_fn(f"  {quest_id}  {title or '(untitled)'}  [{status or '-'}]")
        return REPROMPT
    if cmd == "abandon":
        return "/abandon"
    if cmd == "save":
        return "/save"
    if cmd == "clear":
        return "/clear"
    if cmd == "model":
        if not args:
            print_model_roster(change=change, output_fn=output_fn)
            return REPROMPT
        err = validate_model_arg(args, change=change)
        if err is not None:
            output_fn(err)
            return REPROMPT
        return f"/model {args}"
    if cmd == "resume":
        if not args:
            output_fn("Usage: /resume <id|query>")
            return REPROMPT
        from imp.cli.main import _quest_home, _iter_quest_ids
        from imp.engine.archive import read as archive_read
        from imp.engine.archive import record, records_root
        control = change.control_root
        matches = [cid for cid in _iter_quest_ids(control) if args in cid]
        if len(matches) != 1:
            if not matches:
                output_fn(f"No change matches {args!r}.")
            else:
                output_fn(f"{args!r} is ambiguous: {', '.join(matches)}")
            return REPROMPT
        target = matches[0]
        if _quest_home(control, target).exists():
            output_fn(f"{target} is live; continue it with `imp resume {target}`.")
            return REPROMPT
        rec = record.read_record(records_root(control), target)
        if rec is None or not rec.get("resumable"):
            output_fn(f"{target} is not resumable.")
            return REPROMPT
        archive_read.rehydrate_change(control, target)
        output_fn(f"Rehydrated {target}; continue it with `imp resume {target}`.")
        return REPROMPT

    try:
        body = load_user_skill(change.workflow_dir / "skills", cmd)
    except SkillError:
        # Not a builtin or known skill. Rather than erroring, submit the line
        # verbatim as ordinary input — so a pasted absolute path like
        # `/home/user/foo.py` reaches the agent unchanged instead of being
        # misread as `no such skill: /home/...`.
        return text

    change.event_log().append(
        agent_id=None, type="skill_load",
        payload={"name": cmd, "source": "human"},
    )
    return f"{body.rstrip()}\n\n{args}\n" if args else body


def _list(skills_dir: Any, out: Callable[[str], None]) -> None:
    skills = list_user_skills(skills_dir)
    if not skills:
        out("No skills available in this workflow.")
        return
    out("Available skills (load with /<name>):")
    for name, description in skills:
        out(f"  /{name}    {description}" if description else f"  /{name}")


def _help(out: Callable[[str], None]) -> None:
    out("Available slash commands:")
    out("  /help              Show this message")
    out("  /skills            List loadable skills")
    out("  /list [-a]         List your changes (-a adds completed/abandoned)")
    out("  /search <query>    Search archived changes by content, title, or id")
    out("  /save              Save this conversation to your Backlog")
    out("  /clear             Park this chat and start a fresh one")
    out("  /model <model>     Switch this step's model (or provider:model)")
    out("  /abandon           Abandon the current change")
    out("  /resume <id|query> Rehydrate a saved/archived conversation to continue")
    out("  /<name> [args]     Load a skill (trailing args appended to it)")
    out("")
    out("Keybindings:")
    out("  Ctrl+C / Ctrl+D    Pause and exit; resume with `imp resume <id>`")
    out("  Up-arrow           Recall previous input")
    out("  Tab                Complete a /command or skill name")


async def _ask_or_exit(prompt: Any, exit_evt: Any, *, question: str = "",
                       width: int = 80, indent: str = "",
                       bottom_toolbar: Any = None, hint: str | None = None) -> str:
    """Read a line, but cancel the prompt if the coordinator exit event fires
    (SIGTERM / out-of-band SIGINT / escalation). On cancellation, raise
    KeyboardInterrupt so the CLI's pause-and-exit arm finalizes status=paused.

    The cancellation originates in THIS function's own inner task group, which
    cancels itself; by the time anything downstream (e.g. a bare-turn-end
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
                result["line"] = await prompt.ask_async(
                    question=question, width=width, indent=indent,
                    bottom_toolbar=bottom_toolbar, hint=hint)
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
    prompt: Any, status: Any | None, change: Any, *, color: bool = False, width: int = 80,
    coord: Any = None, driver: Any = None, hint: str | None = None,
) -> str:
    """Read one interactive line, intercepting slash commands. /help, /skills,
    unknown, and not-found commands print and re-prompt; /<name> returns the
    resolved skill body; anything else returns as-is.

    Framing: no chrome around the field. While the user types, the slate input
    field marks the turn; on submit prompt_toolkit erases its region
    (erase_when_done) and the live driver reprints the entered line with the
    bright-blue `> ` caret. The gray bracketing rules are gone — the caret is
    the marker now.

    `coord` (the run's InterruptCoordinator) marks the prompt turn via
    `set_at_prompt` so an out-of-band SIGINT routes through the exit path;
    None (tests / non-interactive) = no-op.

    `driver` (the run's LiveDriver) is called for prepare_for_prompt before each
    prompt iteration and flush_deferred once in the finally on solicit exit.

    Shared by the run and resume paths so the slash/skills logic exists once."""

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
        # Park the cbreak Ctrl-D reader (restoring termios) for the duration of
        # the prompt so prompt_toolkit owns the tty: a Ctrl-D AT the prompt then
        # flows to prompt_toolkit (EOFError -> leave-to-prompt), not the reader.
        # No-op when no reader (or coord) is attached.
        tq = getattr(coord, "tty_quiesced", None) if coord is not None else None
        quiesce_cm = tq() if tq is not None else contextlib.nullcontext()
        async with quiesce_cm:
            # Hoist status.suspend() across the WHOLE solicit loop — one suspend
            # entry/exit per user turn, not per iteration. /help, /skills, and
            # not-found messages print via handle_slash_line, and the live status
            # display must stay suspended while they do, or it interleaves/clobbers
            # them before the loop re-prompts.
            suspend_cm = (
                status.suspend() if status is not None
                else contextlib.nullcontext())
            with suspend_cm:
                indent = driver.prompt_indent() if driver is not None else ""
                try:
                    while True:
                        if driver is not None:
                            driver.prepare_for_prompt()
                        toolbar = (
                            driver.compose_toolbar_static() if driver is not None
                            else compose_identity_toolbar(
                                quest_id=change.id, title=change.title,
                                color=color, width=width))
                        # _exit_event is None until C4 wires it; getattr is defensive
                        # for pre-C4 builds, where the read takes the unraced path.
                        exit_evt = getattr(coord, "_exit_event", None) if coord else None
                        try:
                            if exit_evt is None:
                                line: str = await prompt.ask_async(
                                    width=width, indent=indent, bottom_toolbar=toolbar,
                                    hint=hint)
                            else:
                                line = await _ask_or_exit(
                                    prompt, exit_evt, width=width, indent=indent,
                                    bottom_toolbar=toolbar, hint=hint)
                        except KeyboardInterrupt:
                            # No ctrl-c gesture reaches this arm anymore: an out-of-band
                            # SIGINT at the prompt is fed as a SYNTHETIC ctrl-c by the
                            # coordinator's _at_prompt branch (clear line / ctrl-d hint, no
                            # request_exit, no KBI — ctrl-c never exits at a prompt), and a
                            # TYPED ctrl-c is handled by prompt.py's c-c binding (no KBI).
                            # This arm is a defensive backstop for an UNEXPECTED bare
                            # KeyboardInterrupt from any other path: idempotently re-assert
                            # pause_reason ("ctrl-c", first-wins) and re-raise so the CLI's
                            # except-KeyboardInterrupt arm runs pause_and_exit. The finally
                            # still resets _at_prompt.
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
                            print("")  # one blank between slash output and the reprompt
                            continue
                        assert isinstance(result, str)  # skill body or a literal command
                        return result
                finally:
                    # Flush deferred live-driver blocks exactly once on solicit exit
                    # (normal return, EOFError, or any exception).
                    if driver is not None:
                        driver.flush_deferred()
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
