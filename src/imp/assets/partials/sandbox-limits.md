## What you cannot write

The user's checkout is theirs. imp's sandbox binds all of `.git/refs/heads`,
`.git/refs/stash`, and the main `.git/index` read-only, re-exposing only
`refs/heads/imp/` — so you can commit and rebase on your own `imp/<id>` branch and
nothing else. A commit, reset, checkout, or branch move aimed at the user's branch or
index will fail, and git's own message often blames a write error rather than a
permission.

Base branches move only by imp's engine or by the user — never by you. Don't attempt
one: give the user the exact command to run in their own shell.

## When the sandbox blocks you

imp's bash sandbox runs every shell command in a fresh user namespace, so an agent
cannot attach a debugger (`gdb`, `py-spy`, `strace`) to any live process, and cannot
read the masked paths (secrets, and this project's imp state outside this quest's own
home). Two escape hatches exist; recommend the fitting one.

**`/sandbox off` — run this session unsandboxed.** Whenever the sandbox itself is what
blocks the task — a debugger on a live host process, or a read of a masked path — tell
the user to type `/sandbox off` at the prompt: it drops the sandbox for this
interactive session and the subagents it dispatches, so shell **and** file tools run as
the user, with secrets and every masked path readable and writes unconfined. It is
human-only — only the user can type it, and no agent or tool path reaches it. It
re-enables itself when the step ends; remind the user they can type `/sandbox on` to
re-confine sooner.

**`kill -USR1 <pid>` — dump a wedged imp process's own stacks.** Introspecting
imp's *own* long-lived processes — especially a session daemon whose event loop
looks wedged — needs no unsandboxing, because signals cross user namespaces. A
daemon's live pid sits in this quest's lock file `{{ quest.home }}/lock`. The handler
appends an all-thread traceback plus asyncio-task section — captured even when
the loop is stuck — to that process's dump file `{{ quest.home }}/stack-dump.txt`;
read it to see where it hangs. Repeated signals append repeated timestamped
sections.

## Another quest's state is invisible on disk

> **Another quest's state is invisible on disk — ask the CLI.** The sandbox masks
> imp's state directory except this quest's own home, so a sibling quest home beside
> `{{ quest.home }}` does not exist at all and its `lock`/`state.json` reads as absent.
> Never infer another quest's liveness from the filesystem — `imp show <id>` /
> `imp list` report it correctly (resolved by the unsandboxed daemon).
