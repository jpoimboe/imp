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

imp's sandbox runs all your shell commands — and any program you drive on a
terminal — in one user and pid namespace of their own, so an agent cannot attach
a debugger (`gdb`, `py-spy`, `strace`) to a live process outside it, cannot see
or signal any process it did not start, and cannot read the masked paths
(secrets, and this project's imp state outside this quest's own home). One
escape hatch exists, plus one recipe that needs it.

**`/sandbox off` — run this session unsandboxed.** Whenever the sandbox itself is what
blocks the task — a debugger on a live host process, or a read of a masked path — tell
the user to type `/sandbox off` at the prompt: it drops the sandbox for this
interactive session and the subagents it dispatches,
so shell, file **and terminal** tools run as the user, with secrets and every masked
path readable and writes unconfined. It is human-only — only the user can type it, and
no agent or tool path reaches it. It re-enables itself when the step ends; remind the
user they can type `/sandbox on` to re-confine sooner.

**Stack-dumping a wedged imp process needs `/sandbox off`.** Introspecting imp's *own*
long-lived processes — especially a session daemon whose event loop looks wedged —
means signalling a host pid, which the pid namespace puts out of reach. After
`/sandbox off`, `kill -USR1 <pid>` against the live pid in this quest's lock file
`{{ quest.home }}/lock` appends an all-thread traceback plus asyncio-task section —
captured even when the loop is stuck — to `{{ quest.home }}/stack-dump.txt`. Repeated
signals append repeated timestamped sections.

## imp state on disk is masked — load `imp-knowledge`

> The sandbox masks this project's imp state — sibling quest homes and the archive are
> not on disk at all, so a filesystem search silently answers the wrong question. Never
> read or search other quests' or archived state off the filesystem, and never infer
> another quest's liveness from it. Load `imp-knowledge` for the CLI-based reads that
> answer the question correctly, resolved by the unsandboxed daemon.
