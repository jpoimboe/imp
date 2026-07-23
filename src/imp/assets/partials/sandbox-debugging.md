## Debugging live processes

imp's bash sandbox runs every shell command in a fresh user namespace, so an
agent cannot attach a debugger (`gdb`, `py-spy`, `strace`) to any live process —
not the user's app, not a background process from an earlier shell call, not
another imp instance's daemon. Two escape hatches exist; recommend the fitting
one when a task must introspect a live process.

**`/sandbox off` — run this session unsandboxed.** When a task needs a debugger
on a live host process, tell the user to type `/sandbox off` at the prompt: it
drops the sandbox for this interactive session and the subagents it dispatches,
letting `gdb`/`py-spy`/`strace` attach to same-uid processes. It is human-only —
only the user can type it, and no agent or tool path reaches it. Remind the user
to type `/sandbox on` afterward to re-confine the shell and file tools to this
quest's worktree.

**`kill -USR1 <pid>` — dump a wedged imp process's own stacks.** Introspecting
imp's *own* long-lived processes — especially a session daemon whose event loop
looks wedged — needs no unsandboxing, because signals cross user namespaces. A
daemon's live pid sits in this quest's lock file `.imp/quests/<id>/lock`. The handler
appends an all-thread traceback plus asyncio-task section — captured even when
the loop is stuck — to that process's dump file `.imp/quests/<id>/stack-dump.txt`;
read it to see where it hangs. Repeated signals append repeated timestamped
sections.

## Another quest's state is invisible on disk

> **Another quest's state is invisible on disk — ask the CLI.** The sandbox masks
> all of `.imp` except this quest's own home, so `ls .imp/quests` shows only your
> own quest and a sibling's `lock`/`state.json` reads as absent. Never infer another
> quest's liveness from the filesystem — `imp show <id>` / `imp list` report it
> correctly (resolved by the unsandboxed daemon).
