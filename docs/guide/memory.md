# imp memory

What imp remembers, where it lives, and how to inspect it — imp remembers in two
deliberate ways; neither is *learned* memory (imp does not accumulate lessons on
its own). Pair with `imp-knowledge` for the stable picture.

## Two tiers of durable state

- **Per-quest records** — everything in the quest's home, which lives outside the
  checkout at `~/.imp/projects/<slug>/quests/<id>/` (reached from the quest's own
  worktree as `.imp/quests/<id>/`): the spec, plans,
  summary, the event log (the ordered provenance of what each agent and review
  did), and `state/`. Persistent and per-quest: a left draft resumes with
  `imp resume <id>`, and finished quests stay findable with `imp search`.
- **Project-level config** — the repo's `imp/` config dir, now split by root:
  `imp/config.yaml` in the main checkout, `imp/memory/` in the quest's own
  worktree — the home of standing, human-authored project memory applied to
  every quest:
  - `imp/memory/` — human-authored markdown standing instructions, layered onto
    agents broad → narrow (frozen per run): `imp/memory/all.md` reaches every
    agent, `imp/memory/<workflow>/all.md` every step of that workflow, and
    `imp/memory/<workflow>/<step>.md` just that step. A filename is a step name;
    `all.md` means "everything at this level". HTML `<!-- ... -->` comments are
    stripped before injection; `all.md` is injected into *every* agent, so use
    it extremely sparingly.
  - `default-workflow` / `default-base` — what new quests default to.
  - `test_cmd_fast` / `test_cmd_slow` (and timeouts) — the commands the verify
    gates run. `test_cmd_slow` falls back to `test_cmd_fast` when it names no command of its own.
  - `worktree_setup` — a shell command imp runs in each newly created quest
    worktree, before the first step, for toolchains that keep dependencies in a
    gitignored per-tree directory (`.venv`, `node_modules`, `vendor/bundle`) that
    `git worktree` does not materialize.
  - `pythonpath` — worktree-relative import roots prepended to the gate/agent
    subprocess PYTHONPATH, so an editable-installed package resolves to the
    worktree under test instead of the stale main checkout.

  Those four settings share one three-state shape, because none of them is
  answerable until the repo has a stack: **unset** means never decided, and imp
  raises it once the repo can answer; **`false`** means this project needs none,
  and stays silent forever; anything else is the value. An empty string is not a
  third spelling — it is rejected.

  Other config (providers, tiers, sandbox, `default-workflow`, …) layers across
  `floor ← imp/config.yaml ← ~/.imp/config.yaml ← ~/.imp/projects/<slug>/config.yaml`,
  and both checkout-keyed layers (`imp/config.yaml` and the local
  `~/.imp/projects/<slug>/config.yaml`) are read from the **main checkout** —
  never from a quest's worktree, so a branch cannot change
  the `test_cmd_*`, `pythonpath`, or gate timeouts its own verification is judged
  by. (`~/.imp/config.yaml` is machine-wide and the floor is provider code, so
  neither is keyed to a checkout at all.) `imp/memory/` is the deliberate exception: it is
  read from the quest's own worktree, so a memory quest can validate its own edit
  before it merges. `default-base` lives only in the project layer; `imp/memory/`
  files are not part of the merged config — change them by editing the markdown
  files under `imp/memory/` directly. Change defaults with
  `imp config set <key> <value>` / `imp config unset <key>`, which keeps the
  file's comments and writes each setting to its own layer — the main checkout's
  `imp/config.yaml` for project settings like `default-workflow`,
  `~/.imp/config.yaml` for machine-wide ones like `theme`; `--project` /
  `--global` / `--local` overrides that for anything that is not project-only
  (`--local` writes this checkout's `~/.imp/projects/<slug>/config.yaml`, which
  tops the stack and is never committed). `imp init`
  scaffolds the file — it detects the default workflow and base and confirms them
  on one screen (use `imp init --use-defaults` for a non-interactive CI setup).

## Reading what is active

- **Memory** — read `imp/memory/` in the quest's own worktree directly: the
  markdown there is the standing prose injected into agents (as a
  `## Project memory` section). It is project-only and is **not** shown by
  `imp config`.
- **Merged effective config** — run `imp config` for the resolved providers,
  tiers, `default-workflow`, and test/lint commands, each tagged in brackets with
  the layer it came from. Its first line, `project state:`, names this checkout's
  state directory. Run from inside a worktree, it still reports the main
  checkout's settings. Mind its blind spots: it omits `imp/memory/` and
  `default-base` — read those from the files.

When the user asks what the standing instructions say, read the applicable
`imp/memory/` files and quote them verbatim rather than paraphrasing.

## The memory model in depth

Memory is **additive**, never an override: for a step of workflow `W`, imp
concatenates `all.md`, then `W/all.md`, then `W/<step>.md`. Step names only mean
something inside a workflow, which is why per-step files nest under a
`<workflow>/` directory keyed by the workflow's directory name. To get the real
step names for a workflow, read its `workflow.yaml` (a running quest's snapshot
is at `.imp/quests/<id>/workflow/workflow.yaml` inside its worktree) — never
guess a step name.

imp validates the whole `imp/memory/` tree at load, before any agent runs: a
directory must name a known workflow, and a `.md` under it must be `all` or a
real step of that workflow, or imp fails loudly and lists the valid names. That
error doubles as the live discovery aid for step names and as upgrade-orphan
detection — if an imp upgrade renames or removes a step, the now-stale file
surfaces the same loud error instead of silently going dead. A run **freezes**
its memory at first run, so editing `imp/memory/` mid-run never changes an
in-flight quest; the change takes effect next run.

## Building up memory with imp

The user needn't hand-author these files. In the interactive gates — the
`threshold` chat and the `finalize` merge gate — when the user voices a standing,
cross-quest preference ("always show me exact prose at threshold", "always run X
before planning"), imp offers to record it, working out the narrowest correct target
scope with the user. There are two routes. A **standalone** preference, independent
of any code being built, launches a background `memory` quest that edits the file(s)
off a fresh base tip, validates the tree, and merges through the normal queue,
appearing in the active list — so the user grows memory over time without composing
the markdown by hand. A preference **coupled** to a code change being scoped in the
same conversation is instead folded into that quest's spec as a Memory changes item;
the code quest's implementer makes the edit and a `validate-memory` gate checks the
tree, so the memory and the code land atomically instead of racing. imp writes memory
only with the user's explicit agreement and only ever under `imp/memory/`, never
`.imp/`.

---

*Part of imp's self-knowledge corpus. Runtime entry point: load the `imp-knowledge` skill (`load_skill(name="imp-knowledge")`), the thin index that maps into this corpus.*
