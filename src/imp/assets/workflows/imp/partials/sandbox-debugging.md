## Debugging live processes

imp's bash sandbox runs every shell command in a fresh user namespace, so an agent
cannot attach a debugger (`gdb`, `py-spy`, `strace`) to a live process — not the
user's running app, not a background process from an earlier shell call, and not
another imp instance's daemon. Two escape hatches exist; recommend the fitting one
when a task needs to introspect a live process.

**`/sandbox off` — run this session unsandboxed.** When a task needs a debugger
attached to a live host process, recommend that the user type `/sandbox off` at the
prompt. It runs this interactive session, and the subagents it dispatches, without
the sandbox, so `gdb`/`py-spy`/`strace` can attach to the user's same-uid
processes. You cannot run it yourself: `/sandbox off` is a human-only prompt
command; only the user can type it, and no agent or tool path reaches it. Remind
the user to run `/sandbox on` again once the debugging is finished, to re-confine
the shell and file tools to this quest's worktree.

**`kill -USR1 <pid>` — dump a wedged imp process's own stacks.** To introspect one
of imp's *own* long-lived processes — especially a session daemon whose event loop
looks wedged — no unsandboxing is needed, because signals cross user namespaces.
Send `SIGUSR1` to the process with `kill -USR1 <pid>`; a session daemon's live pid
is in its lock file at `.imp/quests/<id>/lock`. The handler appends an all-thread
traceback plus an asyncio-task section — captured even when the loop is stuck — to
that process's dump file, `.imp/quests/<id>/stack-dump.txt` for a session daemon.
Read that file to see where the process is hung; repeated signals append repeated
timestamped sections.
