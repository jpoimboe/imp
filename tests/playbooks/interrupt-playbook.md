---
description: Fire Ctrl-C / Ctrl-D at imp across agent states, judging clean-pause behavior (live, via the impty driver).
---

# imp interrupt playbook — Ctrl-C / Ctrl-D acceptance test (impty MCP)

> **You are the test driver.** This file is your full instruction set. You drive `imp`
> *through the impty MCP server* (`mcp__impty__start` / `send` / `send_signal` / `view` /
> `wait` / `snapshot` / `stop`) — a real pty with a faithful pyte render — fire interrupts
> (Ctrl-C, Ctrl-D) at well-defined points for a **single provider** and several agent
> states (model thinking, **a tool running**, an in-turn prompt, a subagent, resume), judge
> whether imp behaves and *speaks* correctly, and write every defect to a bug report. This
> playbook drives the **CLI** (cooked-mode) interrupt paths. ESC is a no-op (prompt_toolkit
> owns it at a prompt; ignored while thinking — it never pauses). Work in a throwaway
> standalone `git clone` and
> abandon everything at the end.
>
> This is the acceptance test for imp's **uniform Ctrl-C / Ctrl-D interrupt and resume model**,
> built atop the earlier **esc-and-clean-interrupt** feature. The **"expected" behavior below
> is the uniform model's target**, which **supersedes** esc-and-clean-interrupt's original
> "materialized Ctrl-C pauses and exits (rc 130)" disposition: Ctrl-C now never exits — it drops
> to a resteer/chat prompt and stays attached — and **Ctrl-D** is the uniform leave key, with a
> persisted `resume_target` deciding where `imp resume` lands. If you run an older build that
> predates the uniform model, the Ctrl-C-stays-attached, Ctrl-D-leave, `interrupt_reason ==
> "ctrl-d"`, and resume-target checks are *expected* to fail — record them as to-dos, not
> surprises.
>
> **Scope: one provider per run (default `claude`).** Run this playbook against a single
> provider — `claude` unless the operator names another (`anthropic`, `openai`, or
> `codex`). The feature (and so the §0 "clean classification" bar) covers all four
> identically, so testing one provider end-to-end is a complete pass for that provider; to
> cover another, re-run the whole playbook with that one provider forced (§4). Pick the
> provider **before** §3 and keep it fixed for the entire run.
>
> Read the whole file before you touch a tool. The impty fidelity caveats and the
> throwaway-clone rationale live once in `tests/playbooks/README.md`.

---

## 0. The interrupt triggers and the correctness bar

### 0a. How impty delivers the interrupt triggers

impty claims its pty slave as the child's controlling terminal, so keystrokes you `send` are
delivered to imp's controlling tty. **For a prompt-state Ctrl-C we send a keystroke on purpose** — to
exercise imp's real tty path (prompt_toolkit's `KeyboardInterrupt` at a prompt); a mid-run
Ctrl-C goes through the cooked-mode signal receiver via `send_signal(2)`. There is also an
out-of-band signal tool, `send_signal(sig=…)` (SIGINT=2, SIGTERM=15), which bypasses the tty
and signals imp's process group directly — exactly like a real Ctrl-C. Fire keystroke
interrupts with `wait=False` so they go immediately, then read with `wait` / `view` /
`view(full=True)` / `snapshot`.

> **impty mid-run fidelity caveat.** `send("\x03")` only becomes a SIGINT when the tty line
> discipline has ISIG on (cooked/cbreak); at a prompt_toolkit prompt it is just a raw byte.
> impty also degrades prompt_toolkit's CPR (cursor-position) handshake. Mid-run keystroke
> Ctrl-C delivery is unreliable to validate through the harness: for the cooked-mode points
> (B thinking, C tool-running, E subagent) deliver Ctrl-C deterministically with `send_signal(2)`
> (or `bash: kill -INT -<pgid>`); keep **keystroke** `send` for the prompt-state tests
> (A Ctrl-C/Ctrl-D). If a mid-run keystroke gives a messy result, suspect an impty artifact and
> re-confirm with `send_signal` before filing an imp defect.

> **Ctrl-D mid-run key delivery.** Unlike mid-run Ctrl-C, which needs `send_signal(2)` for
> deterministic delivery, mid-run Ctrl-D does **not** need the out-of-band signal path: a
> dedicated cbreak `/dev/tty` reader reads the `\x04` byte directly (bypassing the cooked-mode
> line discipline). Use `send("\x04", wait=False)` for mid-run Ctrl-D.

| Trigger | `send` data | Path through imp |
|---------|-------------|------------------|
| **Ctrl-C** | `"\x03"` | **At a prompt** it is *intercepted and never raises* (`src/imp/cli/prompt.py`): a non-empty buffer is cleared in place, an empty buffer prints nothing at a gate whose placeholder names Ctrl-D, else `press Ctrl-D to pause & exit` once per prompt object (followed by one blank row) — it does **not** pause. **Mid-run** a real SIGINT (CLI/cooked mode) → the coordinator's loop signal handler (`InterruptCoordinator.run_signal_receiver`, re-armed after every prompt) → `signal_pause("ctrl-c")`. In **both** the **phase-0 chat** and a **materialized** quest it pauses the agent and stays **ATTACHED** (`alive=True`, does not exit) at an interactive prompt — steer (reply resumes the agent) or leave (a subsequent Ctrl-D leaves: on a materialized quest it detaches, rc 130, paused; in phase 0 it takes the save/archive leave disposition and prints that spine); the Ctrl-C **disposition** is identical across regimes and so is the landing prompt: a live mid-turn Ctrl-C on a top-level agent drops to the **inline steer gate** whose placeholder reads `interrupted · enter to continue · type to redirect · Ctrl-D to leave` (`INTERRUPT_STEER_HINT`, `completion.py:51`), in **both** phase-0 and a materialized quest, and prints **no** `⏸ paused (ctrl-c)` glyph line. (The daemon resteer hold prompt `paused · enter to continue · type to redirect · Ctrl-D to leave` is the *resume-to-prompt* surface — shown on `imp resume` after you left the steer gate — not the live mid-turn placeholder; a live workflow-level hold at an agent step reads `interrupted · enter to continue · type to redirect · Ctrl-D to leave` instead.) |
| **Ctrl-D** | `"\x04"` | **At a prompt** → **leave**. **Phase-0** (non-materialized) draft: at the opening prompt with no exchange → discarded: the `discarded` spine `<id> · <name> · discarded` (gold tail), one trailing blank, no caret line, no quest, rc 130; after an exchange → for `imp new`, or an `imp chat` the agent has titled, the `save` disposition, spine `<id> · <name> · paused` + `resume: imp resume <id>` (event `conversation_saved`); for an untitled `imp chat` the `archive` disposition, spine `<id> · <name> · archived` + `resume: imp resume <id> — or find it later with imp search` (event `conversation_archived`) — both rc 130, resumable. A **resumed** phase-0 chat parked at the agent's question lands on the inert prompt, which replays the question: Ctrl-D there prints the three-part `_pause_lines` block — `paused · Ctrl-D` head, `<id> · <name>` identity line, `resume: imp resume <id>` — with no caret line, rc 130, no spine and no event, and the quest stays parked; SIGTERM/SIGHUP there print the same block headed `paused · SIGTERM` / `paused · SIGHUP` (§6 row A). Once you reply — or straight away, if no question was parked — the live session carries the quest's original `imp new`/`imp chat` mode, so later leaves take the same disposition and spine. **On a materialized quest at a prompt** → detaches (rc 130, resumable via `imp resume`), and `imp resume` lands on that prompt (§0b *where you left*): from the inline steer gate after a Ctrl-C, resume_target = "prompt-interrupted" — the daemon resteer hold prompt `paused · enter to continue · type to redirect · Ctrl-D to leave`; from a hold prompt, that same hold prompt; from an agent's question, the question re-asked. **Mid-run on a materialized quest** (delivered as a `\x04` keystroke — see fidelity note above) → **suspend & detach**: tears down the in-flight request/tool/subagent (orphan check clean), emits `workflow_paused` with `interrupt_reason == "ctrl-d"`, rc 130, quest left paused. The guaranteed on-exit terminal print is the three-part `_pause_lines` block — `paused · Ctrl-D` head, `<id> · <name>` identity line, `resume: imp resume <id>` (`main.py`); a human interrupt/leave prints **no** `⏸ paused (ctrl-d)` glyph line in the scrollback or `imp log`. resume_target = None — `imp resume` continues the agent (transcript replays, no intervening prompt). **Mid-run in phase 0** (no Ctrl-C first) → the same leave disposition as at the prompt: `save` or `archive`, or `discard` (the draft is deleted, `discarded` spine) if you haven't sent a message yet, as in an unseeded opening turn. The in-flight request/tool is torn down, the spine prints, and the only event is `conversation_saved{interrupt_reason: "ctrl-d"}` (save) or `conversation_archived` (archive) — no `workflow_paused`, no `interrupt_requested`/`interrupt_delivered`; after a save or an archive, `imp resume` continues the agent. |

> **ESC is a no-op.** A bare ESC byte (`"\x1b"`) never pauses imp: at a prompt prompt_toolkit
> owns it (emacs meta-prefix / vi exit), and while an agent is thinking it is ignored. Do not
> expect a `paused (esc)` line anywhere — if ESC ever pauses a run, that is a regression.

> `SIGTERM` is also part of the feature and is a deliberate **leave**. Deliver it in one call
> with `send_signal(sig=15)` — it signals imp's process group directly, no pid scraping. On a
> materialized quest, its guaranteed on-exit terminal print is the three-part `_pause_lines` leave block — `paused · SIGTERM` head, `<id> · <name>` identity line, `resume: imp resume <id>` (`main.py`); a human leave prints
> **no** `⏸ paused (sigterm)` glyph line in the scrollback or `imp log`. In a live phase-0
> session, SIGTERM (like SIGHUP and SIGQUIT) takes the same leave disposition as a mid-run
> Ctrl-D — the save or archive spine, with `conversation_saved{interrupt_reason: "sigterm"}` on
> a save or `conversation_archived` on an archive. At a pre-session prompt (the opening prompt,
> the `/clear` reopened prompt, or the inert resume prompt) it behaves as §6 row A describes.
> After a mid-run SIGTERM, `imp resume` continues the agent, exactly as after a mid-run Ctrl-D.
> Run it once on a materialized quest's agent step during this run; it is no longer a skippable
> afterthought.

> **Leave-banner name segment.** In the banners above, `<name>` is the quest's resolved
> display name: its title, else the dimmed `<opening-prompt>` fallback, else `(untitled)`
> when it has neither. The segment is always present now — a titleless chat no longer drops
> it (on a materialized quest, a titleless chat with a prompt renders a `paused · Ctrl-D` head
> above `<id> · <prompt…>` — three-part block; a live-session phase-0 leave renders it in the
> spine, `<id> · <prompt…> · paused` or `· archived`).

> **Leave-banner headline — state-first.** The `_pause_lines` leave head leads with the
> **state**: `paused` (gold) followed by a dim ` · <Display>` qualifier naming the trigger —
> `paused · Ctrl-D` for the keystroke leave, `paused · Ctrl-C` for a Ctrl-C leave,
> `paused · /pause` for a typed `/pause` at a built quest's prompt, and
> `paused · SIGTERM` / `paused · SIGHUP` / `paused · SIGQUIT` for signal leaves. The
> internal reason stays lowercase; only the display form differs.
> No leave echoes a caret line of its own; a typed `/pause` keeps its ordinary `> /pause` echo.
> On a terminal, the closing block has exactly one blank row above it.

> **`imp log` tail.** When the history ends on an unresumed manual stop, `imp log` (and
> `--md`/`--html`) ends with one blank line then the same `paused · <Display>` label in the
> lifecycle colour, with no `⏸`. The live view and `imp log -f` never print it, and a resumed stop
> keeps only its `paused · resumed … later` marker.

### 0b. The bar for "correct" (every interrupt point, unless stated otherwise)

A correct imp does **all** of:

- **Stops promptly on one interrupt** — no mashing, no multi-second hang.
- **The interrupt outcome depends on whether the quest has *materialized*** — a real
  worktree/branch created (`change.materialized` in `src/imp/cli/main.py`). Two regimes:
  - **Phase 0 — the threshold interactive chat** (`imp new` / `imp chat` *before* the quest
    materializes; the conversational shaping phase):
    - **Ctrl-C mid-turn** pauses the turn and *returns to an attached steer gate* — it does
      **not** exit and prints **no** `⏸ paused (ctrl-c)` glyph line (a human interrupt renders no
      glyph line in any regime; this is a live cooperative steer, not a workflow-level pause). The turn ends balanced and the prompt
      placeholder becomes `interrupted · enter to continue · type to redirect · Ctrl-D to leave`
      (`INTERRUPT_STEER_HINT`, `completion.py:51`), `alive=True`; from there you **steer** (reply
      → the turn resumes) or **leave** (Ctrl-D). One SIGINT suffices.
    - **Ctrl-D leaves**, disposition by leave-mode × exchange (`alive=False`, rc 130 in every
      case): no exchange → discarded (no quest; the `discarded` spine `<id> · <name> · discarded`,
      one trailing blank, no caret line); `imp new`, or `imp chat` after the agent titled the
      quest, + exchange → the daemon `save` disposition, spine `<id> · <name> · paused` +
      `resume: imp resume <id>` (event `conversation_saved`, resumable — this is NOT the
      three-part `_pause_lines` block, which is materialized-only); an untitled `imp chat` +
      exchange → the `archive` disposition, spine `<id> · <name> · archived` +
      `resume: imp resume <id> — or find it later with imp search` (event
      `conversation_archived`), resumable via `imp resume`.
    - **A mid-run leave** (Ctrl-D, SIGTERM, SIGHUP, SIGQUIT while the agent streams or a tool
      runs, with no Ctrl-C first) takes the same disposition as a prompt-state Ctrl-D —
      including `discard`, which deletes the draft, if you haven't sent a message yet (an
      unseeded opening turn). The in-flight work is torn down; the only event is the
      `conversation_saved` (carrying `interrupt_reason`) or `conversation_archived` — no
      `workflow_paused`, `interrupt_requested`, or `interrupt_delivered`. After a save or an
      archive, `imp resume` continues the agent. **A Ctrl-D at the inline steer gate** (after a
      Ctrl-C) takes the same disposition and prints the same spine (not `_pause_lines`), but the
      log keeps the Ctrl-C's `interrupt_requested`/`interrupt_delivered` and a `workflow_paused`
      relabelled `interrupt_reason == "ctrl-d"`, followed by the `conversation_saved` or
      `conversation_archived`.
  - **A materialized quest** (real in-flight work — the workflow is executing):
    - **Ctrl-C** on a live agent turn drops to the **inline steer gate** whose placeholder reads
      `interrupted · enter to continue · type to redirect · Ctrl-D to leave` (`INTERRUPT_STEER_HINT`,
      `completion.py:51`) — `alive=True`, **ATTACHED**, does **not** exit, and prints **no**
      `⏸ paused (ctrl-c)` glyph line (a human interrupt renders no glyph line in any regime). The
      disposition **and** the placeholder are identical to phase-0 — pause, stay attached,
      steer-or-leave, never exit on one press. (The daemon resteer hold prompt `paused · enter to continue · type to redirect · Ctrl-D to leave` is the *resume-to-prompt* surface —
      what `imp resume` lands on after you detached at the steer gate — not the live mid-turn
      placeholder; it says `paused` because the leave paused the quest.)
    - **Ctrl-D mid-run** suspends the agent and **detaches** (`alive=False`, exit code 130),
      leaving the quest **paused** and resumable via `imp resume <id>` (resume_target =
      None: transcript replays, no intervening prompt). It is **not** moved to the backlog.
      `workflow_paused` records `interrupt_reason == "ctrl-d"`. Delivered as a `\x04`
      keystroke — no `send_signal` needed.
    - **Ctrl-D at the inline steer gate** (after a Ctrl-C pause) → detaches (rc 130, paused);
      `imp resume` lands at the daemon resteer hold prompt
      `paused · enter to continue · type to redirect · Ctrl-D to leave` (resume_target =
      "prompt-interrupted", `awaiting_user_input`).
      `workflow_paused` records `interrupt_reason == "ctrl-d"` (the leave key, not the Ctrl-C
      that opened the gate).
  - The quest is resumable in every case. Read the exit code from the `exit=…` field of the
    impty `view()` header (§5, *Reading state*): every leave above is `exit=130`.
- **No ugly internals leak.** The strings `unclassified provider error` and
  `Cannot write to terminated process` and a `paused: unknown (CLIConnectionError)` headline
  must **not** appear. The interrupt is a clean, expected stop — not an error.
- **The event log records it as an interrupt, not an unknown error.** Two distinct records
  are written on an agent-phase interrupt — don't confuse them:
  - a **`workflow_paused`** event whose `payload.reason` is `"interrupt"` and whose
    `payload.interrupt_reason` carries the key that paused the workflow
    (`ctrl-c|ctrl-d|pause|sigterm|…`) — the leave key when you left from a prompt — written by
    the pause itself;
  - an **`error`** event with `payload.category == "config"` and
    `payload.kind == "interrupted"`, never `category == "unknown"` — written by the provider
    runner when it was mid-flight.

  §5's greps target the **`error`/`interrupted`** record (the classification check). It only
  exists when a runner was actually running (so not at point A — see §6). A direct phase-0
  mid-run leave (no Ctrl-C first) writes neither record — only a `conversation_saved` (save) or
  `conversation_archived` (archive).
- **No orphaned subprocesses** — after interrupting a running tool, no leftover
  `claude` / `codex` / `bash` / `make` / model-transport process keeps running.
- **Is resumable** — `imp resume <id>` continues the quest (same id), reaching a prompt or
  running forward; never `nothing to resume`, never a from-scratch restart.
- **Speaks clearly to the user.** Judge the human-visible text with fresh eyes; an unclear,
  missing, or misleading message **is a bug** even when the exit code is right.

> **Where `imp resume` lands — where you left.** On a materialized quest the landing is
> decided once, at the moment you leave, by where you were — never by the gesture (Ctrl-D,
> `/pause`, SIGTERM, SIGHUP, SIGQUIT, a vanished terminal) or by how the provider's turn ended
> (cooperative stop or hard kill after the 2s deadline):
>
> | Left from | `imp resume` lands on |
> |-----------|-----------------------|
> | mid-run (agent thinking, a tool or subagent running) | the agent, continuing — no intervening prompt |
> | mid-run on a script step (verify gate running) | the gate, re-run — no intervening hold prompt |
> | an agent's question | that question, re-asked; the typed answer reaches the agent |
> | the inline steer gate (after a Ctrl-C) | `paused · enter to continue · type to redirect · Ctrl-D to leave` |
> | a hold prompt (workflow pause, verify gate, or a resumed hold prompt) | that same hold prompt |
>
> Leaving again from the landing re-applies the same rule; a stale landing never outlives the
> step it was recorded for. Headless and autonomous resumes always run the agent.

> **The Ctrl-C disposition is uniform across regimes — and so is the live steer prompt.** In both
> the phase-0 threshold chat and a materialized run, a mid-run Ctrl-C on a top-level agent pauses
> the turn, stays **ATTACHED** (`alive=True`), prints **no** `⏸ paused (ctrl-c)` glyph line, and
> drops to the **inline steer gate** whose placeholder reads `interrupted · enter to continue · type to redirect · Ctrl-D to leave` (`INTERRUPT_STEER_HINT`, `completion.py:51`) — steer (reply resumes)
> or leave (Ctrl-D: on a materialized quest it detaches, rc 130, paused; in phase 0 the
> save/archive leave, printing that spine) — never exiting on a single press. A human
> interrupt/leave prints **no** `⏸ paused (ctrl-c)` glyph line in the scrollback/`imp log` in
> **any** regime — including a **workflow-level pause** (e.g. a verify-gate (script) step). What
> distinguishes a workflow-level pause is the landing surface, not a glyph: it lands on a daemon
> hold prompt (live at an agent step `interrupted · enter to continue · type to redirect · Ctrl-D to leave`,
> or the script variant `paused · enter to re-run the gate · Ctrl-D to leave`),
> which is also the surface `imp resume` lands on when you leave from it; a mid-run leave on a
> script step instead re-runs the gate on resume, with no hold prompt first. On `imp resume` the
> agent-step hold reads `paused · enter to continue · type to redirect · Ctrl-D to leave`, because
> the leave paused the quest. Ctrl-D's phase-0
> disposition is discard / save / archive by leave-mode × exchange; on a materialized quest,
> Ctrl-D at the hold prompt detaches (rc 130, paused, resume_target = "prompt-interrupted" at an
> agent step, "prompt-paused" at a script step) and `imp resume` lands on that same hold prompt.
> A leave from a prompt already under an open pause
> (the hold prompt after a Ctrl-C or gate pause, before any `imp resume`) records
> `interrupt_requested` (`source: "ctrl-d"`) but no second `workflow_paused` — the open-pause
> dedup in `pause_and_exit` (`signals.py`); expected, not a defect. Mid-run Ctrl-D on a materialized quest suspends the
> agent and detaches immediately (rc 130, paused, resume_target = None); its guaranteed on-exit
> print is the three-part `_pause_lines` block (`paused · Ctrl-D` head, `<id> · <name>` identity line, `resume: imp resume <id>`, `main.py`), not the `⏸ paused (ctrl-d)` glyph. In phase 0, a
> mid-run Ctrl-D is a leave like any other: the save or archive spine and only
> `conversation_saved` or `conversation_archived` respectively, no `workflow_paused`. If any
> provider/point does something else, record exactly what it did.

---

## 1. Assumptions (correct me before running if any are wrong)

- **Which `imp`:** the installed `imp` on PATH (a current build off `master`, which carries
  the feature). State the commit you tested in the report.
- **Where:** a fresh standalone `git clone` of this repo (created in §3) — **not** a
  `git worktree`. Provider-forcing (§4) writes the **local** config layer, which imp keys to
  the **control root** and keeps in that project's state directory
  (`~/.imp/projects/<clone-name>/config.yaml`); a linked worktree's `find_control_root`
  resolves to the **main checkout**, so the override would pollute the main tree's state
  directory. A standalone clone's control root is *itself* and claims its own state
  directory, so the override both wins and stays isolated.
  Every `imp new` / `imp resume` runs with cwd inside the clone, so all quests (branches,
  worktrees, journals) are contained and discardable — they live under
  `~/.imp/projects/<clone-name>/`, not inside `$WT`.
- **Config layout:** the `config-file-split` layout — a provider **code floor** (tier→model
  defaults) under three layers: committed `imp/config.yaml` (workflow/lint/test/vars, read
  from the worktree), global `~/.imp/config.yaml` (`providers`/`theme`, per machine), and
  the local `~/.imp/projects/<clone-name>/config.yaml` (keyed to the control root, never
  committed, highest precedence). Precedence
  (low→high): `floor < project < global < local` — operator-wins among the config layers.
- **Provider:** exactly one per run — `claude` by default, or whichever single provider
  the operator names (`anthropic`, `openai`, or `codex`). That one provider is in scope for the
  full §0 classification bar; the others are out of scope for this run.
- **Credentials:** `~/.anthropic-key` → `ANTHROPIC_API_KEY`, `~/.openai-key` →
  `OPENAI_API_KEY`. `claude` / `codex` use their own local auth.
- **impty MCP server** is running from `.mcp.json` with controlling-terminal support.
- **Bug report** goes to `$REPORTS`, outside every checkout (survives clone deletion). Path in §7.

If any of these is wrong, say so and stop.

---

## 2. Preflight (skip-with-a-note, don't fail)

```bash
which imp claude codex
test -r ~/.anthropic-key && echo OK
test -r ~/.openai-key && echo OK
test -f ~/.codex/auth.json && echo OK   # codex auth
```

Provider gating (mirrors imp's `_env_check`):

| Provider      | Needs |
|---------------|-------|
| `claude`      | `claude` on PATH (no key) |
| `anthropic`   | `ANTHROPIC_API_KEY` (from `~/.anthropic-key`) |
| `openai`      | `OPENAI_API_KEY` (from `~/.openai-key`) |
| `codex`       | `codex` on PATH **and** `~/.codex/auth.json` |

For any provider missing a prerequisite: **skip and note it** — not a bug. imp's own startup
env_check is the source of truth: if imp skips or errors a provider at launch, treat that as
the skip signal and note it.

> **`imp init` / global config.** This machine already has a global config
> (`~/.imp/config.yaml`) with detected `providers`/`theme`; the clone inherits it and
> §4's local override forces the one under test. You should **not** need `imp init`. If
> `imp new` prints `imp: no authenticated provider — configured: …`, run `imp init` once
> (idempotent; only bootstraps the global config). Note that same preflight also fires when a
> *forced* provider's env-check fails — there it's a missing-credential **skip** (§2's gating
> table), not an init problem.

> **Cost & time.** `anthropic` / `openai` / `codex` make **real, billable** calls;
> `claude` uses the local `claude` session. Each interrupt point needs only a few seconds
> of real work before you interrupt — never let a step finish. If a run looks stuck/looping
> > ~60s, interrupt it (or `stop` the session) and record it; don't let billable runs spin.

---

## 3. Set up the clone

Use a **standalone `git clone`**, not `git worktree add` — see §1. A clone's control root is
*itself*, so the §4 local-layer provider override both wins (operator-precedence) and stays
isolated; a linked worktree's local layer would resolve to the shared main checkout.

```bash
REPO=$(git rev-parse --show-toplevel)        # main checkout — cloned from; stays clean
REPORTS=${IMP_PLAYBOOK_REPORTS:-${IMP_HOME:-$HOME/.imp}/playbook-reports/$(basename "$REPO")}
mkdir -p "$REPORTS"                          # reports land here, outside every checkout
cd "$REPO"
WT=${TMPDIR:-/tmp}/imp-interrupt-$(date +%Y%m%d-%H%M%S)
git clone "$REPO" "$WT"                 # standalone clone of HEAD → control root resolves to $WT itself
echo "$WT"                              # your cwd for every imp run
ls "$WT"/imp/config.yaml                # the committed project marker → confirms $WT is an imp project
```

(`git clone .` copies committed state at the current branch tip. If you need uncommitted local
changes under test, commit them first or `rsync` them into `$WT` after cloning.)

You force the provider via the clone's local layer — `~/.imp/projects/<clone-name>/config.yaml` — in §4.

---

## 4. Forcing a provider (local layer beats the operator's global config)

imp runs the **first** provider in the resolved `providers:` whose env_check passes. Since
`providers` is **global**-config-homed and precedence is operator-wins (`project < global <
local`), the layer that forces a provider is **local** — `~/.imp/projects/<clone-name>/config.yaml`,
this project's state directory, keyed to the control root. Because `$WT` is a standalone clone,
its control root is `$WT` itself and it claims its own state directory, so this file both
**wins** (highest precedence) and **stays isolated**.

Write the single-provider override (this run targets exactly one provider — `claude` by
default; substitute another only if the operator named it). The `remote: {driver: null}` line
keeps inherited channels off so a channel subprocess's stderr can't interleave with imp's
streaming output:
```bash
cd "$WT" && imp config | sed -n '1p'          # claims this clone's state dir; prints `project state: ~/.imp/projects/<clone-name>`
STATE="${IMP_HOME:-$HOME/.imp}/projects/$(basename "$WT")"   # the same directory, absolute
test -f "$STATE/repo"                         # the claim marker — missing means imp claimed a different slug
cat > "$STATE/config.yaml" <<'YAML'
providers: [claude]        # exactly one of: claude | anthropic | openai | codex
theme: ansi                # fixed theme so snapshots render predictably
remote:
  driver: null             # keep inherited channels off so streaming output stays clean
YAML
```

You do **not** need a `tiers:` block — tier→model bindings come from the provider **code
floor** (current models, shipped with imp). Confirm the override won — and on which layer —
before testing:
```bash
cd "$WT" && imp config                  # the `providers` row must resolve to the provider under test,
                                        # its label reading `[$STATE/config.yaml]` (shown as
                                        # `[~/.imp/projects/<clone-name>/config.yaml]` when IMP_HOME is under $HOME, else absolute)
cd "$WT" && imp config --explain high   # the high-tier model the forced provider will use
```
`imp config` tags each leaf with its source layer in brackets. If the
`providers` row names imp's global `config.yaml` (not `$STATE/config.yaml`), your file landed at the
wrong path (`$WT/imp/config.yaml`, or a state directory other than the one `project state:` names)
and the global layer silently won — fix it before recording anything.
The `--explain` line shows the floor model **before** you spend a billable call.

> Model names drift, but they live in the **code floor**, not this file. If a floor model 404s
> / "unknown model"s at launch, add a single overriding leaf to `$STATE/config.yaml`
> (e.g. `tiers: {high: {openai: <current>}}`) and **note the substitution** — it's not an
> interrupt bug. codex refuses `*-codex` models, so any codex tier override must be a plain
> model (e.g. `gpt-5.5`).

---

## 5. Driving primitives (reusable procedures)

### Spawn imp (`exec` so imp leads the foreground group)

New:
```
mcp__impty__start(argv=["bash","-lc",
  "export ANTHROPIC_API_KEY=$(cat ~/.anthropic-key); export OPENAI_API_KEY=$(cat ~/.openai-key); exec imp new"],
  cwd="<WT>", cols=100, rows=30)
```
Resume:
```
mcp__impty__start(argv=["bash","-lc",
  "export ANTHROPIC_API_KEY=$(cat ~/.anthropic-key); export OPENAI_API_KEY=$(cat ~/.openai-key); exec imp resume <id>"],
  cwd="<WT>", cols=100, rows=30)
```
`exec` is required — without it bash, not imp, leads the foreground group and `\x03` would
not reach imp.

### Reading state

- `view()` → header `[alive=… regime=… cursor=(x,y)]` + grid. `alive=False` = imp exited, and
  the header then carries an `exit=…` field right after it (`exit=0`, `exit=130`, or a signal
  name like `exit=SIGINT`). Cursor parked right after a prompt = waiting for input.
- `view(full=True)` → the whole run so far (scrolled-off history + current screen); the
  three-part leave block and the quest id live here.
- `snapshot()` → PNG; use it to *see* the leave banner's color/layout when judging clarity.
- **Daemon-death triage.** If a pause banner reads `crash: see
  <state dir>/quests/<id>/daemon.stderr` (an absolute path under
  `~/.imp/projects/<clone-name>/`), the per-quest session daemon died rather than pausing
  cleanly — read that stderr file before judging. A daemon death *caused by* an interrupt is a
  defect (record it); one from an unrelated crash is a separate bug, not an interrupt failure.

### **`INTERRUPT-AND-VERIFY(trigger, expected_reason)`** — the core check

1. Deliver one interrupt only: keystroke `send(trigger, wait=False)` for prompt-state tests; for
   a **mid-run** Ctrl-C (points B/C/E) use `send_signal(2)` for deterministic delivery (see the
   §0 fidelity caveat).
2. `wait()` then `view()` + `view(full=True)`.
3. Assert the bar from §0, by regime + trigger:
   - **Mid-run Ctrl-C on a top-level agent (phase-0 or materialized — identical):** imp prints
     **no** `⏸ paused (ctrl-c)` glyph line; it drops to the **inline steer gate** whose
     placeholder reads `interrupted · enter to continue · type to redirect · Ctrl-D to leave`
     (`INTERRUPT_STEER_HINT`), stays **ATTACHED** (`alive=True`) — it does **not** exit. One
     SIGINT suffices (a second within ~1s is suppressed). The placeholder is the same in **both**
     regimes. (A human interrupt/leave prints **no** `⏸ paused (ctrl-c)` glyph line in the
     scrollback or `imp log` in any regime; a workflow-level pause — e.g. a verify-gate step —
     differs only in landing on a daemon hold prompt.) To then **leave**, send
     Ctrl-D and confirm rc 130, `alive=False`, and — on a materialized quest — quest left paused
     and resumable; in phase 0, the save or archive spine (not `_pause_lines`), resumable.
   - **Mid-run Ctrl-D (`\x04` keystroke), materialized:** `alive=False`, rc 130, quest left
     **paused**. The guaranteed on-exit terminal print is the three-part `_pause_lines` block —
     `paused · Ctrl-D` head, `<id> · <name>` identity line, `resume: imp resume <id>` (`main.py`); a
     human interrupt/leave prints **no** `⏸ paused (ctrl-d)` glyph line in the scrollback or
     `imp log` (the `workflow_paused` still records `interrupt_reason == "ctrl-d"`). `imp resume`
     continues the agent (resume_target = None, no intervening prompt). Not in backlog.
   - **Mid-run Ctrl-D, phase 0:** `alive=False`, rc 130, no `workflow_paused`, no
     `interrupt_requested`/`interrupt_delivered`; `imp resume` continues the agent. For `imp new`
     (or a titled `imp chat`): the save spine `<id> · <name> · paused` +
     `resume: imp resume <id>` and `conversation_saved` with `interrupt_reason == "ctrl-d"`. For
     an untitled `imp chat`: the archive spine `<id> · <name> · archived` +
     `resume: imp resume <id> — or find it later with imp search` and a keyless
     `conversation_archived`.
   - **Ctrl-D at the inline steer gate** (after a Ctrl-C): `alive=False`, rc 130, quest
     paused; `imp resume` lands at the daemon resteer hold prompt
     `paused · enter to continue · type to redirect · Ctrl-D to leave` (resume_target = "prompt-interrupted").
     `workflow_paused` records `interrupt_reason == "ctrl-d"` (the leave key, not the Ctrl-C that opened the gate).
   - **Ctrl-D at the inline steer gate, phase 0:** `alive=False`, rc 130; it prints the save
     spine (`conversation_saved` `interrupt_reason == "ctrl-d"`) or, for an untitled
     `imp chat`, the archive spine (`conversation_archived`) — not the `_pause_lines` block. The
     `workflow_paused` `interrupt_reason == "ctrl-d"` above is still written, before the
     `conversation_saved`/`conversation_archived`.
   - **Ctrl-C at a prompt (any regime):** neutered — clears a typed line; on an empty line
     prints nothing at a gate whose placeholder names Ctrl-D, else `press Ctrl-D to pause &
     exit` once per prompt object; `alive=True`, no pause.
   - In all cases: no `unclassified provider error` / `Cannot write to terminated process` /
     `paused: unknown`.
4. **Orphan check:** look for leftover *tool* subprocesses from this run —
   `bash: pgrep -af 'claude|codex|make' | grep "$WT"` — scope to `$WT` so you don't match the
   long-lived impty server, your own driver shells, or unrelated `imp`/`claude` sessions.
   Nothing tied to `$WT` should remain after the pause.
5. **Event-log check:** the quest's event log is at a deterministic path —

   ```bash
   STATE="${IMP_HOME:-$HOME/.imp}/projects/$(basename "$WT")"   # this clone's state dir (`imp config` prints it)
   ev="$STATE/quests/<id>/events.jsonl"
   test -f "$ev" || ev=$(find "$STATE" -path '*'"<id>"'*' -name 'events.jsonl' 2>/dev/null | head -1)  # fallback
   # The interrupt is recorded as a clean PAUSE, never an unknown error. A materialized quest
   # writes a `workflow_paused` carrying the key in `interrupt_reason`. In phase 0, a direct
   # mid-run leave writes no `workflow_paused` and no `interrupt_requested`/`interrupt_delivered`
   # — only a `conversation_saved` carrying the key (or a keyless `conversation_archived`); a
   # phase-0 leave after a Ctrl-C keeps the Ctrl-C's `workflow_paused` (relabelled with the
   # leave key) and appends the `conversation_saved`/`conversation_archived`.
   grep -oE '"type": ?"(workflow_paused|conversation_saved|conversation_archived)"' "$ev" | tail  # the pause/leave record
   grep -oE '"interrupt_reason": ?"[a-z-]+"' "$ev" | tail  # trigger: ctrl-c|ctrl-d|pause|sigterm|sigquit|sighup
   grep -oE '"outcome": ?"(ok|timeout)"' "$ev" | tail      # interrupt_delivered teardown (cooperative→hard ok) (absent on a direct phase-0 leave)
   grep -o '"category":"unknown"' "$ev" | tail             # HARD NEGATIVE: expect NONE from the interrupt
   ```
   > NOTE (2026-07-20): the current build (claude) does **not** emit an `error` /
   > `category:"config"` / `kind:"interrupted"` classification record — it records the interrupt
   > via `interrupt_requested` + `interrupt_delivered{outcome:"ok"}` + `workflow_paused` on a
   > materialized quest or after a phase-0 Ctrl-C (plus a `conversation_saved`/`conversation_archived`
   > when you then leave), and via only a `conversation_saved` (or `conversation_archived`) on a
   > direct phase-0 mid-run leave. The intent of this check — *recorded as an
   > interrupt, never `category:"unknown"`* — holds regardless. Whether the direct-API providers
   > (`anthropic`/`openai`) emit an `error`/`interrupted` record is untested; treat its absence as
   > acceptable, not a failure.
   (`quest_dir` is `<state dir>/quests/<id>` and `events_path` is `events.jsonl` within it. If
   you still can't locate it, fall back to `bash: cd "$WT" && imp log <id>` and judge from what
   it surfaces; note the limitation.)
6. **Record the quest id** for resume + abandon. The authoritative source is
   `bash: cd "$WT" && imp list` — its first column is the quest **id** (also the
   `<state dir>/worktrees/<id>` dir name), which is what the event path and
   `imp resume`/`imp abandon` key on. The `paused` headline should print that same id; if it ever shows a slug or other
   display name instead, that's itself a bug — record it and rely on `imp list` for the id.

### **`RESUME-AND-VERIFY(id)`**

1. `start` `imp resume <id>` (§5). 2. Confirm it continues the *same*
quest (matching `#<handle>` / `· resuming` header), reaches a prompt or runs forward — not
`nothing to resume`, not a restart. 3. Note message clarity. 4. Assert the landing per the
*where you left* table in §0b, for whichever gesture you left with. Check explicitly, at least
once each:
   - **SIGTERM mid-run** (`send_signal(15)`) → `imp resume <id>` **continues the agent**
     (transcript replays, the next agent turn runs) — no hold prompt is shown.
   - **Ctrl-D at the resumed hold prompt** on a materialized quest (after Ctrl-C → inline steer
     gate → Ctrl-D, then `imp resume`) → the next `imp resume <id>` lands on that same hold
     prompt again — **not** the agent.
   - **An answer typed at a re-asked question** (leave at an agent's question, `imp resume`,
     answer it) reaches the agent: it appears in the event log as a `user_message` for the
     step, and no hold prompt intervenes.
(The resume *banner* legitimately shows the `#<handle>` display name; only the *pause* line in
§5.6 must carry the quest **id**.)

---

## 6. Test matrix

For the **single provider under test**, run the points below. Use `INTERRUPT-AND-VERIFY` and
`RESUME-AND-VERIFY`. You don't have to fire every trigger at every point — follow the
**Trigger** column. Keep a tally (point × trigger → outcome) for the report.

| # | Point (imp state) | How to reach it | Trigger(s) | Expected |
|---|-------------------|-----------------|-----------|----------|
| **A** | Opening prompt (pre-quest) | `imp new`; `wait()` until parked at the first prompt | **Ctrl-C**, then in a fresh run **Ctrl-D**, then in fresh runs **SIGTERM** (`send_signal(15)`) and **SIGHUP** (`send_signal(1)`) | **Ctrl-C is neutered at the prompt** — clears a typed line, else prints `press Ctrl-D to pause & exit` once; stays `alive=True`, does *not* pause. **Ctrl-D** at the empty opening prompt (no quest exists yet) → the just-minted draft is **discarded**, no quest created, clean exit (rc 130). The on-exit print is the `discarded` spine `<id> · <name> · discarded` (`<name>` is `(untitled)` with no `--title`), one trailing blank, no caret line — matching the paused/archived/abandoned spine structure; the `imp: cancelled` string does **not** exist. **No `interrupted` event** — nothing was running to classify. **SIGTERM / SIGHUP** at the empty opening prompt leave with nothing to resume, so they behave exactly like Ctrl-D: the draft is discarded, the same `<id> · (untitled) · discarded` spine prints (no ` · SIGTERM` suffix, no `paused · SIGTERM` block), rc 130, and the id is absent from `imp list --all --json`. Dying by the signal (no output, a leftover `(untitled)  new` draft) is the bug. The `/clear` reopened prompt behaves the same way (discarded spine, rc 130). At the inert `imp resume` prompt (a parked quest's pending question), Ctrl-D, SIGTERM and SIGHUP print the three-part `paused · Ctrl-D` / `paused · SIGTERM` / `paused · SIGHUP` block (head, `<id> · <name>` identity line, `resume: imp resume <id>`) with no caret line, no spine and no event, and exit 130; the question stays parked and a later `imp resume` re-asks it. |
| **B** | Model thinking / streaming (phase-0 threshold) | Send a trivial intent (`send("add a one-line comment to the README\r")`); `wait` until output streams (no prompt) | **Ctrl-C**, **Ctrl-D** | **Ctrl-C:** **no** `⏸ paused (ctrl-c)` glyph line; drops to the inline steer gate placeholder `interrupted · enter to continue · type to redirect · Ctrl-D to leave` (`INTERRUPT_STEER_HINT`), stays `alive=True`; steer (reply), or Ctrl-D to leave. Ctrl-C via `send_signal(2)`. **Ctrl-D (mid-run):** `\x04` keystroke (`send("\x04", wait=False)`, no `send_signal`), rc 130, the save spine `<id> · <name> · paused` + `resume: imp resume <id>` (not the `_pause_lines` block; no `⏸ paused (ctrl-d)` glyph), `conversation_saved` `interrupt_reason == "ctrl-d"` and **no** `workflow_paused`, resume-to-agent. In-flight model request torn down (orphan check clean). |
| **C** | **A tool is running** | Steer the agent until it runs a shell/`make`/test tool that takes a few seconds; interrupt during it | **Ctrl-C**, **Ctrl-D** | **Ctrl-C:** **no** `⏸ paused (ctrl-c)` glyph line; drops to the inline steer gate placeholder `interrupted · enter to continue · type to redirect · Ctrl-D to leave` (`INTERRUPT_STEER_HINT`), stays `alive=True`, identical across phase-0 and materialized (pause, attached, steer-or-leave, never exit). **Ctrl-D (mid-run):** `\x04` keystroke (`send("\x04", wait=False)`, no `send_signal`), rc 130, suspend & detach, resume-to-agent. On a materialized quest: on-exit three-part `_pause_lines` block — `paused · Ctrl-D` head, `<id> · <name>` identity line, `resume: imp resume <id>` (no `⏸ paused (ctrl-d)` glyph line renders in the scrollback or `imp log`), `workflow_paused` `interrupt_reason == "ctrl-d"`. In phase 0 (an `imp new` run, a direct leave): the save spine and `conversation_saved` only (as row B). **The tool subprocess is killed** — orphan check must be clean. Ctrl-C via `send_signal(2)`. |
| **D** | In-turn prompt (agent asks *you* mid-turn, or the reply prompt after a Ctrl-C pause) | Use an interactive step where the agent prompts you back, or the reply prompt left by a point-B/C pause | **Ctrl-C**, **ESC** | **Ctrl-C is neutered at this prompt too** — clears a typed line; on an empty line prints nothing at a gate whose placeholder names Ctrl-D (the reply prompt after a Ctrl-C pause), else `press Ctrl-D to pause & exit` once per prompt object; `alive=True`, a normal reply still submits, Ctrl-D leaves. **ESC is a no-op** (prompt_toolkit owns it; `alive=True`). |
| **E** | Subagent running (nested, materialized) | Drive until the workflow dispatches a subagent; interrupt while it is mid-flight | **Ctrl-C**, **Ctrl-D** | **Ctrl-C:** a `sub_agent` is a **tool call of the live top-level (task-lead) turn**, so a human Ctrl-C is a *cooperative steer of that turn* — the subagent is torn down (`interrupt_delivered` fires for **both** the `sub:…` agent and the task-lead, `outcome:ok`) and it drops to the **inline steer gate** `interrupted · enter to continue · type to redirect · Ctrl-D to leave` (`INTERRUPT_STEER_HINT`), `alive=True`, no `⏸ paused (ctrl-c)` glyph, quest **not** exited — **identical to a top-level-agent Ctrl-C** (§0), *not* the daemon hold prompt. A subagent that suspends on its own (provider/budget exhaustion, or a pause landing during its retry backoff) is handed back to the parent turn as a tool error — the parent decides; it never unwinds the run to a hold prompt. **Ctrl-D (mid-run):** `\x04` keystroke (`send("\x04", wait=False)`, no `send_signal`), rc 130, on-exit three-part `_pause_lines` block — `paused · Ctrl-D` head, `<id> · <name>` identity line, `resume: imp resume <id>` (no `⏸ paused (ctrl-d)` glyph line renders in the scrollback or `imp log`), suspend & detach, subagent torn down (orphan check clean), resume-to-agent. Ctrl-C via `send_signal(2)`. |

After the matrix: run `RESUME-AND-VERIFY` on the last paused quest. Then deliver
`send_signal(sig=15)` (SIGTERM) on a materialized quest's running agent step and verify the leave (rc 130): the
guaranteed on-exit print is the three-part `_pause_lines` block — `paused · SIGTERM` head, `<id> · <name>` identity line, `resume: imp resume <id>` (`main.py`); a human leave prints **no** `⏸ paused (sigterm)` glyph
line in the scrollback or `imp log`. Then `imp resume` continues the agent, same as after a mid-run
Ctrl-D: the next agent turn runs, with no hold prompt.

### 6a. Negative / edge-case checks (run at least once across the suite)

These don't need to run at every point — fire each **at least once** somewhere in the suite.

- **Double / mashed Ctrl-C (point B).** With the model streaming, deliver Ctrl-C *twice in
  quick succession* — two `send_signal(2)` back-to-back. Expect a **single clean steer**, not a
  traceback, a double-pause, or a hang: one drop to the inline steer gate placeholder
  `interrupted · enter to continue · type to redirect · Ctrl-D to leave` (no `⏸ paused (ctrl-c)` glyph on
  this top-level steer), back at that attached prompt (`alive=True`; the 2nd SIGINT within ~1s is
  suppressed), resumable.
- **Very-early interrupt (point A window).** Ctrl-C in the brief window right after spawning
  imp, *before* the first prompt/agent appears — `send("\x03", wait=False)` immediately
  after the §5 spawn without waiting for the prompt. Nothing is running, so expect **no
  `interrupted` event**. Record
  whatever happens: clean handling is preferred (a neutered Ctrl-C hint, or a clean
  cancel/exit); a traceback, a hang, or an unkillable process is a bug.
- **Steer after Ctrl-C (point B or C).** The pause-attached model's *other* half: after a
  mid-run Ctrl-C pause, don't leave — type a short redirect at the landing prompt (e.g.
  `stop that — just reply DONE`) and submit. Expect the agent to **resume in the same attached
  session and visibly follow the redirect**; the run must **not** end `paused` (the inline
  steer consumes the pause), and no duplicate re-run of the interrupted work. A steer that
  dead-ends, is ignored, or leaves the quest paused anyway is a bug.
- **Escalation force-quit — automated, skip it here.** A second Ctrl-C more than 1s after the
  first, while the agent is still stopping, hard-kills it (the `stopping…` ack becomes
  `force-quitting…`) and lands on `interrupted · enter to continue · type to redirect · Ctrl-D to
  leave`, still attached. No real provider holds that window open (claude and the bash tool stop
  in milliseconds; anthropic/openai have no hard kill), so
  `test_ctrl_c_force_quit_vs_deadline` in `tests/impty/test_e2e_interrupt.py` covers it on the
  scripted provider (`interrupt: hard_kill`). Leave it out of the tally; a second press that
  only reaches the steer gate here is expected, not a defect.
- **Ctrl-\ deliberate detach (once).** Mid-run, deliver SIGQUIT — `send_signal(3)` for
  determinism (keystroke `send("\x1c", wait=False)` as a bonus check; same impty fidelity
  caveat as mid-run Ctrl-C). imp traps SIGQUIT (replacing the core-dump default): expect the
  same teardown as a mid-run Ctrl-D — detach, quest paused on a materialized quest (in phase 0, the
  save/archive leave — `conversation_saved` or `conversation_archived`), resumable, orphan-free,
  rc 130, and
  `imp resume` continues the agent, same as after a mid-run Ctrl-D. A `Quit (core dumped)` or
  an unhandled-signal death is a bug.
- **SIGHUP / closed terminal (once).** `send_signal(1)` mid-run. SIGHUP is a first-class
  leave gesture (Intent B, same path as SIGTERM): expect detach, the quest paused on a
  materialized quest (the session daemon's lease rule decides, not the client) — in phase 0,
  the save/archive leave (`conversation_saved` or `conversation_archived`), resumable, no
  orphans, rc 130, and
  `imp resume` continues the agent, same as after a mid-run Ctrl-D. This is the "user closed
  the terminal tab" case. At a pre-session prompt — the `imp new` / `imp chat`
  opening prompt or the `/clear` reopened prompt — SIGHUP or SIGTERM instead discards the
  empty draft (`<id> · (untitled) · discarded`, rc 130); at the inert `imp resume` prompt it
  prints the `paused · SIGHUP` / `paused · SIGTERM` block (no caret line) and exits 130 — the
  same block Ctrl-D prints there as `paused · Ctrl-D` — the quest still parked; a later
  `imp resume` re-asks its question (see row A).
- **Out-of-band SIGINT at a prompt (once).** While parked at a prompt, fire
  `send_signal(2)` — *not* the keystroke. An OS-level SIGINT at a prompt is **neutered
  identically to a typed Ctrl-C**: imp routes it through the prompt's own Ctrl-C binding
  (clear a typed line; on an empty line print nothing at a gate whose placeholder names Ctrl-D,
  else `press Ctrl-D to pause & exit` once per prompt object) and deliberately does **not**
  `request_exit` (`signals.py`: "Ctrl-c NEVER exits at a prompt … do NOT request_exit"). Expect
  `alive=True`, no pause, no detach/leave, and no traceback — only **Ctrl-D** pauses-and-exits at
  a prompt. The keystroke and the OS signal are *supposed* to behave **identically** here; a
  divergence (a clean detach/leave on the bare OS SIGINT) is itself the bug.
- **Interrupt racing the finish.** Fire Ctrl-C or Ctrl-D as late as you can — while the
  workflow is merging/finalizing after its last step. A terminal or blocked status
  (`completed`, `saved`, `merge_blocked`, `abandoned`) must never be clobbered to `paused`:
  expect either a clean pause before the merge or the completion winning — never a
  half-merged tree or a completed quest that reports paused.
- **Pause → resume → pause again (any point).** After a successful `RESUME-AND-VERIFY`,
  interrupt the resumed run again. Expect a second clean pause — on a materialized quest, a
  fresh `workflow_paused` appended *after* the `workflow_resumed` (the open-pause dedup must not
  swallow it); in phase 0, a direct mid-run leave instead appends a fresh `conversation_saved`
  or `conversation_archived` after the `workflow_resumed` — and a second resume that still
  works.

---

## 7. Report

Write a report to `$REPORTS` (outside every checkout; survives clone deletion):

```
"$REPORTS"/<YYYY-MM-DD>-interrupt-playbook-results.md
```

For each point and trigger, record: the state reached, the trigger delivered, the full
`view(full=True)` snippet (or a cited snapshot), whether the §0 bar passed, and any
deviation from expected behavior. Save a cited snapshot with `snapshot(path=…)` under
`"$REPORTS"/<YYYY-MM-DD>-interrupt-shots/` (create it only if you cite one; pass the literal
absolute path — the tool expands no variables and creates no parent dirs) and link it
relatively, e.g. `![point C leave banner](<YYYY-MM-DD>-interrupt-shots/point-c.png)`.

Include:
- **Build under test:** `git -C "$REPO" rev-parse --short HEAD`
- **Provider:** which one and which tier/model resolved (`imp config --explain high`)
- **Tally:** a per-point × per-trigger result table (PASS / FAIL / SKIP + one-line note)
- **Defects:** for each failure, the exact sequence of events, the expected vs actual behavior,
  and a reproduction recipe

A **clean run still produces a report** — write "no defects observed" explicitly. A passing run
with no artifact is the most likely failure of this convention.

---

## 8. Cleanup and wrap-up

```bash
cd "$WT" && imp abandon <id>   # abandon any open quests in the clone (get <id> from §5.6 / imp list)
STATE="${IMP_HOME:-$HOME/.imp}/projects/$(basename "$WT")"
[ -d "$WT" ] && [ "$(cat "$STATE/repo" 2>/dev/null)" = "$(cd "$WT" && pwd -P)" ] && rm -rf "$STATE"   # this clone's state only
rm -rf "$WT"
git -C "$REPO" status --short   # must print nothing — reports live in $REPORTS
```

If it prints anything, flag it.

Stop with a one-paragraph summary: build tested and provider, tally of PASS/FAIL/SKIP
per point, headline defects (or "none"), the report's absolute path.

---

## 9. Quick reference

| Interrupt | `send` | Out-of-band | What fires |
|-----------|--------|-------------|------------|
| **Ctrl-C** (at prompt) | `"\x03"` | — | intercepted — clears the line; an empty line prints nothing at a gate whose placeholder names Ctrl-D, else `press Ctrl-D to pause & exit` once per prompt object; never pauses |
| **Ctrl-C** (mid-run) | unreliable | `send_signal(2)` | `SIGINT` → cooperative steer → stay attached (`alive=True`), never exit, **no** `⏸ paused (ctrl-c)` glyph; placeholder `interrupted · enter to continue · type to redirect · Ctrl-D to leave` (both regimes). No `⏸ paused (ctrl-c)` glyph renders in any regime; the daemon hold prompt is what marks a workflow pause (e.g. a verify gate; a human Ctrl-C during a subagent steers to the inline gate — see Point E) |
| **Ctrl-D** (at prompt / steer gate) | `"\x04"` | — | `EOFError` → phase-0 leave (`discarded` / `paused` / `archived`, rc 130); **materialized at inline steer gate → detach** (rc 130, resume-to-prompt) |
| **Ctrl-D** (`imp chat`, after an exchange, untitled) | `"\x04"` | — | `archive` disposition → **`archived`** spine, rc 130, resumable via `imp resume` (titled: `save`, `paused` spine) |
| **Ctrl-D** (mid-run) | `"\x04"` | — | cbreak `/dev/tty` reader reads byte directly — no `send_signal` needed; suspend & detach (rc 130, resume-to-agent). Materialized: `workflow_paused` `interrupt_reason == "ctrl-d"`; on-exit print: three-part `_pause_lines` block — `paused · Ctrl-D` head, `<id> · <name>` identity line, `resume: imp resume <id>` (no `⏸ paused (ctrl-d)` glyph line renders in the scrollback or `imp log`); phase 0, live session: the save or archive spine; a direct leave (no Ctrl-C first) writes only `conversation_saved` (save) or `conversation_archived` (archive), and a leave after a Ctrl-C also keeps the Ctrl-C's `workflow_paused`; discarded if you haven't sent a message yet. Phase-0 pre-session prompts (opening, `/clear`, inert resume): §6 row A |
| **Ctrl-C ×2** (>1s apart, mid-teardown) | — | — | automated only (`tests/impty/test_e2e_interrupt.py`): hard-kills the stopping agent, ack `force-quitting…`, stays attached at the `interrupted ·` prompt; real providers stop too fast to reach it |
| **Ctrl-\** (mid-run) | `"\x1c"` (bonus) | `send_signal(3)` | SIGQUIT trap → deliberate detach; same teardown as mid-run Ctrl-D (paused on a materialized quest, resumable, rc 130, resume-to-agent); phase 0, live session: the save or archive spine; a direct leave (no Ctrl-C first) writes only `conversation_saved` (save) or `conversation_archived` (archive), and a leave after a Ctrl-C also keeps the Ctrl-C's `workflow_paused`; discarded if you haven't sent a message yet. Phase-0 pre-session prompts (opening, `/clear`, inert resume): §6 row A |
| **SIGTERM** | — | `send_signal(15)` | Intent-B leave → detach (rc 130; mid-run: resume-to-agent, like mid-run Ctrl-D); on a materialized quest or at the phase-0 inert resume prompt, on-exit three-part `_pause_lines` block — `paused · SIGTERM` head, `<id> · <name>` identity line, `resume: imp resume <id>` (no `⏸ paused (sigterm)` glyph line renders in the scrollback or `imp log`); phase 0, live session: the save or archive spine; a direct leave (no Ctrl-C first) writes only `conversation_saved` (save) or `conversation_archived` (archive), and a leave after a Ctrl-C also keeps the Ctrl-C's `workflow_paused`; discarded if you haven't sent a message yet. Phase-0 pre-session prompts (opening, `/clear`, inert resume): §6 row A |
| **SIGHUP** | — | `send_signal(1)` | Intent-B leave (terminal closed) → detach (rc 130; mid-run: resume-to-agent, like mid-run Ctrl-D); materialized: daemon lease rule pauses; phase 0, live session: the save or archive spine; a direct leave (no Ctrl-C first) writes only `conversation_saved` (save) or `conversation_archived` (archive), and a leave after a Ctrl-C also keeps the Ctrl-C's `workflow_paused`; discarded if you haven't sent a message yet. Phase-0 pre-session prompts (opening, `/clear`, inert resume): §6 row A |
