# Knowledge Map: How imp's Self-Knowledge is Organized

This document is contributor guidance for routing new knowledge to the right home
when doing imp-on-imp work. imp uses three knowledge layers; putting content in the
wrong layer means agents either can't reach it or carry it unnecessarily.

---

## The Three Layers

### 1. Skills

Portable, on-demand procedural helpers — tool mechanics, terminal idioms, TDD
discipline, debugging checklists. Skills ship inside the package and are available
in every project an agent runs in. An agent loads a skill explicitly when it needs
the procedure.

Bundled skills are overlaid into each quest's workflow snapshot (under the quest's `workflow/skills/`), so an agent can load them without a repo checkout — the deliberate contrast with the `docs/` corpus, which is read in place at `{{ docs.path }}` and never copied into a snapshot.

Keep skills **thin**. They are not a general corpus; do not grow a comprehensive
reference here.

### 2. The `docs/` Corpus

imp's **descriptive** self-knowledge — what imp is, how it works, its invariants,
its conventions. This directory (`docs/`) is imp's own repo, but agents in any
project can read it via the engine-injected `{{ docs.path }}` path variable, which
resolves to the corpus **in place** rather than copying files into quest snapshots.
That is why portability of the corpus does not require bundling it into the package:
the path is always resolved in place at runtime.

### 3. Workflow Prompts

Agent system prompts and partial templates, snapshotted per quest. These carry
**step-specific**, operative "how this agent does X right now" instructions.
Procedure that must actually reach a running agent lives here — not in a doc that
would only be read if the agent explicitly loads it.

---

## Routing Rule

| Content type | Where it belongs |
|---|---|
| Descriptive ("what / how imp works") | `docs/` corpus |
| Portable procedural helper (reusable across steps) | Skill |
| Step-specific operative procedure | Workflow prompt (agent / partial) |

When in doubt: if it describes imp, it's corpus; if it's a portable reusable helper,
it's a skill; if it's tied to a specific agent's job, it's a workflow prompt.

---

## Skills as a Thin Index

`imp-knowledge` and similar hub skills act as a **thin index** into the corpus — they
map topics to the right `docs/` files rather than duplicating content. This is the
hub-as-index pattern: the skill names where to look, the corpus contains the detail.
Deep hubs that reproduce corpus content violate this pattern and cause drift.
