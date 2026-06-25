# imp invariants

Read this when editing imp's engine/renderer or workflow prompts.

## Layering invariants

- Workflow agent prompts describe test/build **intent, not a runner** — imp
  targets any language. Say "the project's own test tooling", use
  `$IMP_TEST_COMMAND` / `$IMP_LINT_COMMAND`, and let the agent discover the real
  command. Illustrative code is allowed only with a disclaimer that it is
  illustrative. Exempt: imp's *own* tooling — `integrator` /
  `integration-review` reading `change.json`, and `finalize`'s `PYTHONPATH` /
  impty usage.
- The engine (`executor`) and the renderer (`narrative.py` / `render_text.py` /
  `live_driver.py`) expose only **generic primitives**: step kinds, and phases
  *derived* from each step's declared `phase` string. No hardcoded step, phase,
  or var names and no routing live in engine or renderer code — all specifics
  live in `workflow.yaml`, scripts, and prompts.

## Future / unbuilt

- **Memory retention (when implemented):** if imp ever adds memory/learnings
  storage, persist only (1) durable user preferences and (2) non-obvious
  invariants no test guards. Never persist "X landed/fixed/approved" status
  snapshots or branch-tied state — they rot fast.
- **User-level guidance channel (P1):** `guidance` is project-only today
  (`src/imp/providers/effective.py`, read via `parse_project_facts` and never
  merged through the floor → project → user → local layers). A maintainer's
  cross-project preferences have no home but per-repo `project.yaml`. Candidate
  feature: a user-level `guidance:` in `~/.config/imp/config.yaml`, merged into
  the prompt's `{{ project.guidance }}`.
