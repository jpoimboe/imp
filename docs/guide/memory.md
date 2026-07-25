# imp memory

What imp remembers, where it lives, and how to inspect it — imp remembers in two
deliberate ways; neither is *learned* memory (imp does not accumulate lessons on
its own). Pair with `imp-knowledge` for the stable picture.

## Two tiers of durable state

- **Per-quest records** — everything under `.imp/quests/<id>/`: the spec, plans,
  summary, the event log (the ordered provenance of what each agent and review
  did), and `state/`. Persistent and per-quest: a left draft resumes with
  `imp resume <id>`, and finished quests stay findable with `imp search`.
- **Project-level config** — the repo's `imp/` config dir (`imp/project.yaml`
  plus `imp/memory/`), the home of standing, human-authored project memory
  applied to every quest:
  - `imp/memory/` — human-authored markdown standing instructions, layered onto
    agents broad → narrow (frozen per run): `imp/memory/all.md` reaches every
    agent, `imp/memory/<workflow>/all.md` every step of that workflow, and
    `imp/memory/<workflow>/<step>.md` just that step. A filename is a step name;
    `all.md` means "everything at this level". HTML `<!-- ... -->` comments are
    stripped before injection; `all.md` is injected into *every* agent, so use
    it extremely sparingly.
  - `facts` — a `{key: value}` map any prompt reads as `{{ project.<key> }}`;
    also project-only.
  - `default-workflow` / `default-base` — what new quests default to.
  - `test_cmd_fast` / `test_cmd_slow` (and timeouts) — the commands the verify
    gates run.

  Other config (providers, tiers, sandbox, …) layers across
  `floor ← imp/project.yaml ← ~/.imp/config.yaml ← .imp/config.yaml`;
  `facts` and the workflow/base defaults live only in the project layer;
  `imp/memory/` files live in the repo's `imp/` config dir, not the merged config;
  change them by editing the markdown files under `imp/memory/` directly. Changing
  `facts` or defaults means editing `imp/project.yaml` by hand — there is no
  `imp config set`; `imp init` scaffolds the file — its interactive setup picks the
  default workflow and base (use `imp init --use-defaults` to skip the conversation
  in CI).

## Reading what is active

- **Memory and facts** — read the repo's `imp/` config dir directly: markdown
  under `imp/memory/` is the standing prose injected into agents (as a
  `## Project memory` section), and `imp/project.yaml`'s `facts:` is the
  `{{ project.<key> }}` map. Both are project-only and are **not** shown by
  `imp config`.
- **Merged effective config** — run `imp config` for the resolved providers,
  tiers, and test/lint commands with per-setting provenance. Mind its blind spots:
  it omits `imp/memory/`, `facts`, and the `default-workflow` / `default-base`
  defaults — read those from the files.

When the user asks what the standing instructions say, read the applicable
`imp/memory/` files and quote them verbatim rather than paraphrasing.

## The memory model in depth

Memory is **additive**, never an override: for a step of workflow `W`, imp
concatenates `all.md`, then `W/all.md`, then `W/<step>.md`. Step names only mean
something inside a workflow, which is why per-step files nest under a
`<workflow>/` directory keyed by the workflow's directory name. To get the real
step names for a workflow, read its `workflow.yaml` (a running quest's snapshot
is at `.imp/quests/<id>/workflow/workflow.yaml`) — never guess a step name.

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
