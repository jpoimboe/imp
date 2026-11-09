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

**Coupled to code you're scoping? Fold it in, don't background it.** If the edit
must land in lockstep with a code change you're scoping in this same conversation —
memory and code depend on each other, whichever motivates which — a separate
background quest would merge on its own schedule and race the code. Instead record
the edit in this quest's spec as a **Memory changes** item under Scope, naming the
exact `imp/memory/` file(s) and the verbatim edit (add / change / remove). The
implementer makes it alongside the code and a validation gate checks the tree before
merge, so the two land atomically. An edit that stands independent of any code being
built takes the background route below.

**Resolve the scope with the user, then launch a background quest.** Once the user
is happy with the target scope, run
`imp new --workflow memory --background --autonomous --title "<short title>" "<a clear plain-language intent, including the resolved target scope and whether to add, change, or remove>"`. You pass **intent, not file bytes**: the background `memory`
quest resolves the exact file(s), makes the edit off a fresh base tip, validates the
tree, and lands it through the merge queue. It appears in the **active** list, so
tell the user it is running and will merge on its own.

**Guardrails.** Never record memory without the user's agreement, never with a raw
`git` command, and only ever under `imp/memory/`, never `.imp/`. A standalone
preference is written in the background and takes effect on future runs, not the one
you're in; a coupled edit rides this quest's spec and lands with its code.
