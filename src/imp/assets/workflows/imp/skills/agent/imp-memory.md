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
  `floor ← imp/project.yaml ← ~/.config/imp/config.yaml ← .imp/config.yaml`;
  `facts` and the workflow/base defaults live only in the project layer;
  `imp/memory/` files live in the repo's `imp/` config dir, not the merged config;
  change them by editing the markdown files under `imp/memory/` directly. Changing
  `facts` or defaults means editing `imp/project.yaml` by hand — there is no
  `imp config set`; `imp init` scaffolds the file.

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
