# imp memory

What imp remembers, where it lives, and how to inspect it — imp remembers in two
deliberate ways; neither is *learned* memory (imp does not accumulate lessons on
its own). Pair with `imp-knowledge` for the stable picture.

## Two tiers of durable state

- **Per-quest records** — everything under `.imp/quests/<id>/`: the spec, plans,
  summary, the event log (the ordered provenance of what each agent and review
  did), and `state/`. Persistent and per-quest: a left draft resumes with
  `imp resume <id>`, and finished quests stay findable with `imp search`.
- **Project-level config** — `imp/project.yaml` at the repo root, the one file of
  standing, human-authored project memory applied to every quest:
  - `guidance` — a freeform prose block the engine **auto-injects into every
    agent's system prompt** (frozen per run). The deliberate channel for a
    preference that should hold across all quests; lives only in `project.yaml`.
  - `facts` — a `{key: value}` map any prompt reads as `{{ project.<key> }}`;
    also project-only.
  - `default-workflow` / `default-base` — what new quests default to.
  - `test_cmd_fast` / `test_cmd_slow` (and timeouts) — the commands the verify
    gates run.

  Other config (providers, tiers, sandbox, …) layers across
  `floor ← imp/project.yaml ← ~/.config/imp/config.yaml ← .imp/config.yaml`;
  `guidance`, `facts`, and the workflow/base defaults live only in the project
  layer. Changing any of it means editing `imp/project.yaml` by hand — there is no
  `imp config set`; `imp init` scaffolds the file.

## Reading what is active

- **Guidance and facts** — read `imp/project.yaml` at the repo root directly. Its
  `guidance:` block is the prose injected into every agent; `facts:` is the
  `{{ project.<key> }}` map. Both are project-only and are **not** shown by
  `imp config`.
- **Merged effective config** — run `imp config` for the resolved providers,
  tiers, and test/lint commands with per-setting provenance. Mind its blind spots:
  it omits `guidance`, `facts`, and the `default-workflow` / `default-base`
  defaults — read those from the file.

When the user asks what the guidance says, quote it verbatim rather than
paraphrasing.
