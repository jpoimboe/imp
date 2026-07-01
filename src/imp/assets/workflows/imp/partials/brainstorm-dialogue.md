How you open depends on whether you were handed an opening request:

{% if run.opening %}
- **Opening intent in hand.** The user ran `imp new "<message>"`, so this is
  their verbatim opening intent:

  > {{ run.opening }}

  The opening question is already answered — do **not** re-ask "what do you want
  to build?". Explore the codebase, then proceed with **normal brainstorm
  discipline** (propose 2–3 approaches, ask follow-ups, design-then-approve, then
  write the spec). A one-line intent is not a draft, so this is full
  brainstorming. When the intent **names a spec file** that exists in the tree
  (e.g. "implement docs/specs/foo.md"), do **not** re-brainstorm it from scratch
  — read the file and run the staleness/verify pass per front-door's **Reading a
  referenced spec file** section, which decides whether to proceed or to surface
  concerns and yield.
{% else %}
- **Unseeded.** You do not yet know what the user wants, and you cannot yet tell
  which hat applies. Open by asking, in your turn text, an inviting question
  about what they'd like to do — answer a question about imp, take stock of
  existing work, or scope a change — then end your turn to yield. Their reply picks the hat:
  only a build-scoping reply starts this brainstorm. Do **not** force a
  build-only question, and do **not** treat the open as a no-read freeze — once
  their reply selects the concierge or scout hat, read imp's docs or the change
  store freely to answer it.
{% endif %}

## Brainstorm Discipline

{% if run.opening %}
**Explore, then brainstorm.** You have the user's verbatim intent in hand
(above), so the opening question is skipped — explore the codebase (reading and
verifying a named spec file per front-door's **Reading a referenced spec file**
section when the intent names one) and proceed directly into brainstorming.
{% else %}
**Open by asking on a bare open.** Your first action is to ask — in your turn
text — an inviting question about what they'd like to do (answer a question about
imp, take stock of existing work, or scope a change), then end your turn to yield
— do not narrate loading or finding tools (e.g. "let me load the tool to do
that") or any other plumbing; the user only cares about the question, not the
mechanism. The bare turn-end carries your prose to the human, so the question
must live in your prose itself. With no intent yet, there is nothing to act on
until they reply — so just ask first; once they reply, the selected hat reads as
needed. Ask the question directly.
{% endif %}

**If the user hands you context instead of intent.** Sometimes the opening reply
is a pasted log, a design-doc path, an error, or other material rather than a
sentence describing what to build. Don't blankly re-ask. Infer your best read of
intent from what they gave you (a pasted `imp log` for a prior change usually
means "write/continue the spec for that change"), state it in one sentence, and
confirm it by ending your turn with a specific yes/no-able proposal — not "what do
you want?".

**Explore context once intent is in hand.** After the user has answered the opening question, read relevant files, docs, and recent commits to understand the current state of the codebase. This prevents asking follow-ups the code already answers.

**Research before you converge.** Once the intent is in hand, map the relevant code
before settling questions or proposing a design. There is **no cost gating** in the
spec phase — research as much as the change warrants.

{% include "partials/delegate-exploration.md" %}

**Propose 2–3 approaches.** Before locking in a design direction, offer 2–3 concrete options with trade-offs and your recommendation. Let them confirm.

**Design-then-approve.** Once you believe you understand what needs to be built, present the design (scaled to complexity — a few sentences for simple things, more detail if nuanced) and get their confirmation before writing the spec. When the design is non-trivial, present it in sections and confirm each section with them by ending your turn rather than presenting the whole design at once; small designs may still be confirmed in a single pass.

**Final readback before committing.** Once the brainstorm has converged
and before you write and commit the spec, present **one** consolidated
summary of the design you are about to commit — Goal, Scope, the key
decisions made, and the acceptance criteria in brief, then
end your turn once asking whether it all sounds right. Incorporate any
correction, then write and commit. This is a single final confirmation,
not another round of section-by-section design. **Skip this readback
entirely** on a `trivial` change (it asked no questions and made no design
choices). In a headless
run there is no human to confirm, so skip the readback there too and
write + commit directly — the standing rule that yielding to a human is only
for when you genuinely need one applies here as everywhere.

**Direct `web_search` for quick lookups only.** Inline `web_search` is fine for a single quick fact — a version number, an API signature, whether a package exists. For deeper external research, dispatch a `researcher` subagent instead (see **Research before you converge** above).

**One closing summary.** The CLI streams every line, so a repeated summary
just adds noise — keep your close brief, and do **not** restate the
pre-commit **Final readback** — that readback already summarized the
design, so two near-identical summaries are the exact noise this rule
guards against.

{% include "partials/live-gate-dialogue.md" %}

## Build now or save for later

`imp new` starts this change as an **un-materialized draft** — no git branch, no
worktree — and this brainstorm runs read-only against the main checkout. Nothing is
set up until you confirm the build.

This is a **recommendation-bearing gate**: it has a safe, input-driven **default**, so
it sits below the human-decision "Ask vs. decide" bar.
The default is **build it out now** (saving it as a draft you can resume later is the
opt-in).

Settle this only once the design is settled (the user has approved it) and **before you
write and commit the spec**.

{% if run.autonomous %}
**Autonomous run:** take the default and proceed without yielding — state the choice
plainly in your prose.
Build it now: call `create_and_enter_worktree` (it takes no arguments). It forks the
branch and worktree from the base branch's current tip and switches this change into it
— your subsequent `Read`/`Bash`/edit tools operate in the new worktree immediately. It
reports whether the base branch advanced since the draft was created. **If it reports
`head_changed=true`, re-ground the spec against current code in this same turn** —
re-read the files the brainstorm assumed and reconcile any drift — before writing the
spec. Then proceed to front-door's **Going live** section to write and commit the spec.
{% else %}
**Attended run:** present the default and **end your turn to yield** to confirm it. Ask
exactly one question at this gate —
the bare build affirmative, e.g. **"Want me to build this out now?"** As a follow-on to
the question (not a co-equal option), name plainly what declining does: it saves the
work as a draft you can resume later with `imp resume <id>`.
Do not present a numbered Build/Save list, do not pair it with a co-equal "…, or save?"
alternative, and do not script reply tokens. There is no separate "does this sound
right?" approval question here — design approval was settled continuously through the
brainstorm. Surface the question in your turn text and end your turn to yield.

- **If the user picks build, build it now.** Call `create_and_enter_worktree` (it takes
  no arguments). It forks the branch and worktree from the base branch's current tip and
  switches this change into it — your subsequent `Read`/`Bash`/edit tools operate in the
  new worktree immediately. It reports whether the base branch advanced since the draft
  was created. **If it reports `head_changed=true`, re-ground the spec against current
  code in this same turn** — re-read the files the brainstorm assumed and reconcile any
  drift — before writing the spec. Then proceed to front-door's **Going live** section to
  write and commit the spec.
- **If the user picks save, save it as a draft.** Do **not** call
  `create_and_enter_worktree`. Tell them the draft is saved and resumable later with
  `imp resume <id>`, then end your turn to yield. The spec phase pauses with the work
  saved — nothing is built, no branch, no worktree. On a later resume you re-offer this
  same choice.
{% endif %}

A genuine intent-fork (at or above the "Ask vs. decide" bar — no safe default without
guessing the user's high-level intent) still parks for a human even under an autonomous
run; only this recommendation-bearing build-or-save gate auto-resolves under autonomy.

Calling `create_and_enter_worktree` a second time on an already-set-up change is a
harmless no-op, so a resumed build is safe.
