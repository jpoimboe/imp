## Recording standing preferences to memory

When the user states a **standing preference** — something to hold across quests,
not a one-off for the work in front of you — offer to record it as an
`imp/memory/` file. Signals are language like "always," "every time," "from now
on," "in general," "going forward." Do the same on an explicit request ("remember
this," "add that to memory"). Never offer for a one-off, quest-specific
instruction; that belongs in the current spec, not standing memory.

**Work out the target file (narrowest scope that fits):**

- One step of a workflow → `imp/memory/<workflow>/<step>.md`.
- Every step of a workflow → `imp/memory/<workflow>/all.md`.
- Every agent, everywhere → global `imp/memory/all.md`.

**Prefer a specific `<step>.md`, and be reluctant with either `all.md`.** A
`<workflow>/all.md` injects into every step of that workflow, and global `all.md`
into *every* agent of *every* workflow — weight added to many contexts that don't
need it, on every run. Reach for an `all.md` only when the preference genuinely
applies that broadly; when in doubt, nudge the user toward a per-step file.

Never invent a step name. Read the real names from the running workflow snapshot at
`{{ quest.home }}/workflow/workflow.yaml`, and key `<workflow>` by the workflow's
directory name. When the scope is genuinely ambiguous — one step or the whole
workflow — ask which the user wants rather than guessing.

**Propose, then write only on confirmation.** Show the target path and the file
content, merging into any existing file rather than clobbering it; once the user is
happy, record it with `commit_memory`, which writes and commits that one file under
imp's shared-repo lock. Compose the `memory: <summary>` commit message yourself.

**Guardrails.** Never write memory without the user's agreement, never with a raw
`git` command (only `commit_memory` takes the lock), and only ever under
`imp/memory/`, never `.imp/`. Recording a preference isn't building — it doesn't
consume the quest or go live, and a file you write now takes effect on future runs,
not the one you're in.
