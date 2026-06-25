# imp

Coding agents are becoming **loops** — systems that run themselves, deciding what
to do next and when they're done. Most loops trend toward something you can't see
into: code written, reviewed, and maintained by a machine, in a form no human still
understands. The loop works until the day it doesn't, and then no one can say why.

**imp is the loop made legible.** A deterministic engine drives a declarative
workflow; the model is summoned one step at a time with fresh, tightly-scoped
context. The path is written down, every byte is logged, and the human stays at the
controls — approving, redirecting, or stopping the work at gates that are part of
the design, not bolted on.

You work at the level of **intent**. imp owns the mechanism: spec, plan, implement,
review, integrate — each step isolated, auditable, and resumable, in its own git
worktree, until it reaches you for a decision. It's the difference between software
as a machine you understand and software as an organism you can only treat the
symptoms of.

## Intent-driven development

You describe *what* you want changed and *why*. imp owns the *how* — the mechanism
of turning that intent into a reviewed, integrated change. You are not prompting a
chat window and hoping; you state intent and approve the result at points you
control.

The distinction is the whole stance. The alternative is software as an organism:
grown rather than built, understood by no one, kept alive by treating symptoms.
Intent-driven development keeps software a machine — something you direct, inspect,
and can still reason about after the agent has done its work.
