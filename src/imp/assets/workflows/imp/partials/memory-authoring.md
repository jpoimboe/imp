## Recording standing preferences to memory

When the user expresses a **standing preference** — something they want to hold
across quests, not a one-off instruction for the work in front of you — offer to
record it as an `imp/memory/` file. Signals are language like "always," "every
time," "from now on," "in general," "going forward." Also do this on an explicit
request ("remember this," "add that to memory"). Do **not** offer for a one-off,
quest-specific instruction; that belongs in the current spec, not standing memory.

**Work out the target file (narrowest scope that fits):**

- One step of a workflow → `imp/memory/<workflow>/<step>.md` (e.g. "always show
  me exact prose at the threshold step" → `imp/memory/imp/threshold.md`).
- Every step of a workflow → `imp/memory/<workflow>/all.md`.
- Every agent, everywhere → `imp/memory/all.md`.

**Prefer a specific `<step>.md`, and be reluctant with either `all.md`.** Both
are easy to overuse and costly: `<workflow>/all.md` is injected into every step
of the workflow, and global `all.md` into *every* agent of *every* workflow —
each adds weight to many contexts that don't need it, on every run. Reach for an
`all.md` only when the preference genuinely applies that broadly; when in doubt,
a per-step file is almost always the right call, so nudge the user that way
rather than defaulting to a broad file for convenience.

Never invent a step name. Read the real names from the running workflow's
`workflow.yaml` (the workflow snapshot at `{{ quest.home }}/workflow/workflow.yaml`),
and key `<workflow>` by the workflow's directory name. When the right scope is
genuinely ambiguous — the preference could be one step or the whole workflow —
ask which the user wants rather than guessing.

**Propose, then write only on confirmation.** Show the user what you'd write —
the target path and the file content, merging into any existing file rather than
clobbering it — and once they're happy, record it with `commit_memory`, which
writes and commits that single file under imp's shared-repo lock. Compose the
`memory: <summary>` commit message yourself; no need to run it by the user.

**Guardrails.** Never write memory without the user's agreement, never with a raw
`git` command (only `commit_memory` takes the lock), and only ever under
`imp/memory/`, never `.imp/`. Recording a preference isn't building anything — it
doesn't consume the quest or go live, and a file you write now takes effect on
future runs, not the one you're in.
