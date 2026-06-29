---
description: Fire Ctrl-C / Ctrl-D at imp across agent states and surfaces, judging clean-pause behavior (live, via the impty driver).
---

# imp interrupt playbook — Ctrl-C / Ctrl-D acceptance test (impty MCP)

> **You are the test driver.** This file is your full instruction set. You drive `imp`
> *through the impty MCP server* (`mcp__impty__start` / `send` / `send_signal` / `view` /
> `wait` / `snapshot` / `stop`) — a real pty with a faithful pyte render — fire interrupts
> (Ctrl-C, Ctrl-D) at well-defined points for a **single provider** and several agent
> states (model thinking, **a tool running**, an in-turn prompt, a subagent, resume), judge
> whether imp behaves and *speaks* correctly, and write every defect to a bug report. This
> playbook is **surface-selectable**: run it against the **CLI** (cooked-mode) interrupt paths
> or the **TUI** (`--tui`, raw-mode) interrupt paths — the surface flips which keys are
> faithful (§0). ESC is a no-op in both modes (prompt_toolkit owns it at a prompt; ignored
> while thinking — it never pauses). Work in a throwaway standalone `git clone` and
> abandon everything at the end.
>
> This is the acceptance test for imp's **uniform Ctrl-C / Ctrl-D interrupt and resume model**,
> built atop the earlier **esc-and-clean-interrupt** and **tui-run-viewer** features (the TUI
> mode covers the `--tui` interrupt bindings from the latter). The **"expected" behavior below
> is the uniform model's target**, which **supersedes** esc-and-clean-interrupt's original
> "materialized Ctrl-C pauses and exits (rc 130)" disposition: Ctrl-C now never exits — it drops
> to a resteer/chat prompt and stays attached — and **Ctrl-D** is the uniform leave key, with a
> persisted `resume_target` deciding where `imp resume` lands. If you run an older build that
> predates the uniform model, the Ctrl-C-stays-attached, Ctrl-D-leave, `interrupt_reason ==
> "ctrl-d"`, and resume-target checks are *expected* to fail — record them as to-dos, not
> surprises.
>
> **Scope: one provider per run (default `claude_code`).** Run this playbook against a single
> provider — `claude_code` unless the operator names another (`anthropic`, `openai`, or
> `codex`). The feature (and so the §0 "clean classification" bar) covers all four
> identically, so testing one provider end-to-end is a complete pass for that provider; to
> cover another, re-run the whole playbook with that one provider forced (§4). Pick the
> provider **before** §3 and keep it fixed for the entire run.
>
> Read the whole file before you touch a tool. The impty fidelity caveats and the
> throwaway-clone rationale live once in `tests/playbooks/README.md`.

---

## 0. Mode select, the interrupt triggers, and the correctness bar

### 0a. Surface mode — CLI or TUI

Pick the surface **before** §3 and keep it fixed for the whole run:

- **CLI mode** (the existing cooked-mode paths) — spawn `imp new` / `imp resume` (no `--tui`).
  A mid-run Ctrl-C keystroke is unreliable, so use `send_signal(2)`. ESC is a no-op (at a
  prompt prompt_toolkit owns it; while thinking it is ignored — it never pauses). Use the
  **CLI spawn** (§5).
- **TUI mode** (the `--tui` full-screen viewer, raw mode) — spawn `imp new --tui` /
  `imp resume <id> --tui`. The app owns the tty in raw mode and binds the keys itself, and the
  **Ctrl-C keystroke `\x03` is faithful**. ESC is a no-op (the app does not bind it). Use the
  **TUI spawn** (§5).

The surface flips the interrupt semantics:

| Aspect | CLI mode (existing) | TUI mode (new) |
|---|---|---|
| Ctrl-C mid-run | `send_signal(2)` (keystroke unreliable) | keystroke `\x03` is faithful |
| Reader path | cooked-mode signal receiver | app owns raw mode |

### 0b. The interrupt triggers and how impty delivers them

impty claims its pty slave as the child's controlling terminal, so keystrokes you `send` are
delivered to imp's controlling tty. **For Ctrl-C we send a keystroke (TUI) on purpose** — to
exercise imp's real tty paths (prompt_toolkit's `KeyboardInterrupt` at a prompt, the
cooked-mode signal receiver in CLI mode, the app key bindings in TUI mode). There is also an
out-of-band signal tool, `send_signal(sig=…)` (SIGINT=2, SIGTERM=15), which bypasses the tty
and signals imp's process group directly — exactly like a real Ctrl-C. Fire keystroke
interrupts with `wait=False` so they go immediately, then read with `wait` / `view` /
`view(full=True)` / `snapshot`.

> **impty mid-run fidelity caveat (mode-dependent).** `send("\x03")` only becomes a SIGINT when
> the tty line discipline has ISIG on (cooked/cbreak); at a prompt_toolkit prompt it is just a
> raw byte. impty also degrades prompt_toolkit's CPR (cursor-position) handshake.
> - **CLI mode:** mid-run keystroke Ctrl-C delivery is unreliable to validate through the
>   harness. For the cooked-mode points (B thinking, C tool-running, E subagent) deliver Ctrl-C
>   deterministically with `send_signal(2)` (or `bash: kill -INT -<pgid>`); keep **keystroke**
>   `send` for the prompt-state tests (A Ctrl-C/Ctrl-D). If a mid-run keystroke gives a messy
>   result, suspect an impty artifact and re-confirm with `send_signal` before filing an imp
>   defect.
> - **TUI mode:** the full-screen app owns the tty in **raw mode** and binds the keys itself,
>   so `send("\x03")` (Ctrl-C) is delivered as a **key** and is the correct test — **not**
>   `send_signal`. Caveat: impty can drop the odd keystroke — re-send once, record only if it
>   persists.

> **Ctrl-D mid-run key delivery (both surfaces).** Unlike mid-run Ctrl-C, which needs
> `send_signal(2)` in CLI mode for deterministic delivery, mid-run Ctrl-D does **not** need
> the out-of-band signal path. In CLI mode a dedicated cbreak `/dev/tty` reader reads the `\x04`
> byte directly (bypassing the cooked-mode line discipline); in TUI mode the app already owns
> the tty in raw mode. Use `send("\x04", wait=False)` for mid-run Ctrl-D on **both** surfaces.

| Trigger | `send` data | Path through imp |
|---------|-------------|------------------|
| **Ctrl-C** | `"\x03"` | **At a prompt** it is *intercepted and never raises* (`src/imp/cli/prompt.py`): a non-empty buffer is cleared in place, an empty buffer prints `press ctrl-d to pause & exit` once per session — it does **not** pause. **Mid-run** a real SIGINT (CLI/cooked mode) → `anyio.open_signal_receiver` → `InterruptCoordinator.signal_pause("ctrl-c")`. In **both** the **phase-0 chat** and a **materialized** Change it pauses the agent and stays **ATTACHED** (`alive=True`, does not exit) at an interactive prompt — steer (reply resumes the agent) or leave (a subsequent Ctrl-D detaches, rc 130, paused); the Ctrl-C **disposition** is identical across regimes, but the landing prompt **text** differs: **phase-0** → its chat `> type your reply` prompt; **materialized** → the resteer hold prompt `paused — type a message to redirect, or detach to leave it paused`. In TUI mode the app's key binding drives the same coordinator. |
| **Ctrl-D** | `"\x04"` | **At a prompt** → **leave**. **Phase-0** (non-materialized) draft: at the opening prompt with no exchange → `imp: cancelled` (no Change); after an exchange → `imp: saved '<id>' to your Backlog` (`imp new`) / `conversation saved` (`imp chat`), resumable. **On a materialized Change at a prompt** (e.g. the resteer hold prompt after a Ctrl-C) → detaches (rc 130, resumable via `imp resume`); resume_target = "prompt" — `imp resume` lands back at the resteer hold prompt. **Mid-run** (both surfaces, delivered as `\x04` keystroke — see fidelity note above) → **suspend & detach**: tears down the in-flight request/tool/subagent (orphan check clean), emits `workflow_paused` with `interrupt_reason == "ctrl-d"`, rc 130, Change left paused; resume_target = "agent" — `imp resume` continues the agent (transcript replays, no intervening prompt). |

> **ESC is a no-op.** A bare ESC byte (`"\x1b"`) never pauses imp: at a prompt prompt_toolkit
> owns it (emacs meta-prefix / vi exit), and while an agent is thinking it is ignored. Do not
> expect a `paused (esc)` line anywhere — if ESC ever pauses a run, that is a regression.

> `SIGTERM` → `paused (sigterm)` is also part of the feature. Deliver it in one call with
> `send_signal(sig=15)` — it signals imp's process group directly, no pid scraping. Run it
> once where convenient during this run; it is no longer a skippable afterthought.

### 0c. The bar for "correct" (every interrupt point, unless stated otherwise)

A correct imp does **all** of:

- **Stops promptly on one interrupt** — no mashing, no multi-second hang.
- **The interrupt outcome depends on whether the Change has *materialized*** — a real
  worktree/branch created (`change.materialized`, `src/imp/cli/main.py`:433). Two regimes:
  - **Phase 0 — the front-door interactive chat** (`imp new` / `imp chat` *before* the Change
    materializes; the conversational shaping phase):
    - **Ctrl-C mid-turn** pauses the turn and *returns to the chat* — it does **not** exit.
      imp prints `⏸ paused (ctrl-c)` (`⏸` shows only with color; otherwise the same text
      without the glyph) and drops back to its live `> type your reply` prompt (`alive=True`);
      from there you **steer** (reply → the turn resumes) or **leave** (Ctrl-D). One SIGINT
      suffices.
    - **Ctrl-D leaves**, disposition by leave-mode × exchange (`alive=False`): no exchange →
      discarded → `imp: cancelled` (no Change); `imp new` + exchange → `imp: saved '<id>' to
      your Backlog (resume with imp resume <id>)`; `imp chat` + exchange → `imp: conversation
      saved — resume with imp resume <id>` (archived).
  - **A materialized Change** (real in-flight work — the workflow is executing):
    - **Ctrl-C** drops to the resteer hold prompt `paused — type a message to redirect, or
      detach to leave it paused` (`alive=True`, **ATTACHED** — does **not** exit). The Ctrl-C
      **disposition** now matches phase-0 — pause, stay attached, steer-or-leave, never exit on
      one press — but the prompt text is regime-specific: phase-0 lands at its chat
      `> type your reply` prompt; materialized lands here, at the resteer hold prompt.
    - **Ctrl-D mid-run** suspends the agent and **detaches** (`alive=False`, exit code 130),
      leaving the Change **paused** and resumable via `imp resume <id>` (resume_target =
      "agent": transcript replays, no intervening prompt). It is **not** moved to the Backlog.
      `workflow_paused` records `interrupt_reason == "ctrl-d"`. Delivered as `\x04` keystroke
      on both surfaces — no `send_signal` needed.
    - **Ctrl-D at the resteer hold prompt** (after a Ctrl-C pause) → detaches (rc 130, paused);
      `imp resume` lands back at the resteer hold prompt (resume_target = "prompt",
      `awaiting_user_input`).
  - The Change is resumable in every case; the exit code is not directly observable through
    impty — judge from `alive=False` + the final line. In **TUI mode** the alt-screen restores
    on the clean pause/leave: `regime` goes `screen` → `scroll`, and the final line lands in
    normal scrollback.
- **No ugly internals leak.** The strings `unclassified provider error` and
  `Cannot write to terminated process` and a `paused: unknown (CLIConnectionError)` headline
  must **not** appear. The interrupt is a clean, expected stop — not an error.
- **The event log records it as an interrupt, not an unknown error.** Two distinct records
  are written on an agent-phase interrupt — don't confuse them:
  - a **`workflow_paused`** event whose `payload.reason` is `"interrupt"` and whose
    `payload.interrupt_reason` carries the trigger (`ctrl-c|ctrl-d|sigterm`) — written by
    the pause itself;
  - an **`error`** event with `payload.category == "config"` and
    `payload.kind == "interrupted"`, never `category == "unknown"` — written by the provider
    runner when it was mid-flight.

  §5's greps target the **`error`/`interrupted`** record (the classification check). It only
  exists when a runner was actually running (so not at point A — see §6).
- **No orphaned subprocesses** — after interrupting a running tool, no leftover
  `claude` / `codex` / `bash` / `make` / model-transport process keeps running.
- **Is resumable** — `imp resume <id>` continues the Change (same id), reaching a prompt or
  running forward; never `nothing to resume`, never a from-scratch restart.
- **Speaks clearly to the user.** Judge the human-visible text with fresh eyes; an unclear,
  missing, or misleading message **is a bug** even when the exit code is right.

> **The Ctrl-C disposition is now uniform across regimes.** In both the phase-0 front-door chat
> and a materialized run, a mid-run Ctrl-C pauses the agent, stays **ATTACHED** (`alive=True`),
> and drops to an interactive prompt — steer (reply resumes) or leave (Ctrl-D detaches, rc 130,
> paused) — and never exits on a single press. The **prompt text differs by regime**: phase-0
> lands at its chat `> type your reply` prompt; a materialized Change lands at the resteer hold
> prompt `paused — type a message to redirect, or detach to leave it paused`. Ctrl-D's phase-0
> disposition is discard / Backlog / archive by leave-mode × exchange; on a materialized Change,
> Ctrl-D at the hold prompt detaches (rc 130, paused, resume_target = "prompt"). Mid-run Ctrl-D
> (either regime) suspends the agent and detaches immediately (rc 130, paused, resume_target =
> "agent"). If any provider/point does something else, record exactly what it did.

---

## 1. Assumptions (correct me before running if any are wrong)

- **Which `imp`:** the installed `imp` on PATH (a current build off `master`, which carries
  the feature). State the commit you tested in the report.
- **Surface:** CLI or TUI per §0, fixed for the run. **TUI mode** additionally requires the
  `--tui` flag present (`imp new -h | grep -- --tui`) and `TERM` exported in the spawn (§5).
- **Where:** a fresh standalone `git clone` of this repo (created in §3) — **not** a
  `git worktree`. Provider-forcing (§4) writes the **local** config layer `.imp/config.yaml`,
  which imp reads from the **control root**; a linked worktree's `find_control_root` resolves
  to the **main checkout**, so the override would pollute the shared main tree. A standalone
  clone's control root is *itself*, so `$WT/.imp/config.yaml` both wins and stays isolated.
  Every `imp new` / `imp resume` runs with cwd inside the clone, so all Changes (branches,
  nested worktrees, journals) are contained and discardable.
- **Config layout:** the `config-file-split` layout — a provider **code floor** (tier→model
  defaults) under three layers: committed `imp/project.yaml` (workflow/lint/test/vars, read
  from the worktree), user `~/.config/imp/config.yaml` (`providers`/`theme`, global), and
  gitignored `.imp/config.yaml` (local, control root, highest precedence). Precedence
  (low→high): `floor < project < user < local` — operator-wins among the config layers.
- **Provider:** exactly one per run — `claude_code` by default, or whichever single provider
  the operator names (`anthropic`, `openai`, or `codex`). That one provider is in scope for the
  full §0 classification bar; the others are out of scope for this run.
- **Credentials:** `~/.anthropic-key` → `ANTHROPIC_API_KEY`, `~/.openai-key` →
  `OPENAI_API_KEY`. `claude_code` / `codex` use their own local auth.
- **impty MCP server** is running from `.mcp.json` with controlling-terminal support.
- **Bug report** goes to the **main checkout** (survives clone deletion). Path in §7.

If any of these is wrong, say so and stop.

---

## 2. Preflight (skip-with-a-note, don't fail)

```bash
which imp claude codex
test -r ~/.anthropic-key && echo OK
test -r ~/.openai-key && echo OK
test -f ~/.codex/auth.json && echo OK   # codex auth
imp new -h | grep -q -- --tui && echo "tui-flag present" || echo "NO --tui (old build)"  # TUI mode only
```

Provider gating (mirrors imp's `_env_check`):

| Provider      | Needs |
|---------------|-------|
| `claude_code` | `claude` on PATH (no key) |
| `anthropic`   | `ANTHROPIC_API_KEY` (from `~/.anthropic-key`) |
| `openai`      | `OPENAI_API_KEY` (from `~/.openai-key`) |
| `codex`       | `codex` on PATH **and** the codex SDK importable in imp's venv **and** `~/.codex/auth.json` |

For any provider missing a prerequisite: **skip and note it** — not a bug. imp's own startup
env_check is the source of truth: if imp skips or errors a provider at launch, treat that as
the skip signal and note it. **TUI mode:** if `--tui` is absent you're on a pre-feature build —
record the TUI suite as expected-fail.

> **`imp init` / user config.** This machine already has user config
> (`~/.config/imp/config.yaml`) with detected `providers`/`theme`; the clone inherits it and
> §4's local override forces the one under test. You should **not** need `imp init`. If
> `imp new` prints `✗ no provider available — run `imp init` first`, run `imp init` once
> (idempotent; only bootstraps user config). Note that same preflight also fires when a
> *forced* provider's env-check fails — there it's a missing-credential **skip** (§2's gating
> table), not an init problem.

> **TUI mode — remote channels.** If your *user* config enables a `remote.driver`, the clone
> inherits it and the channel subprocess's stderr corrupts the alternate screen. Add
> `remote: {driver: null}` to the §4 config for the TUI run, and confirm `imp config` shows the
> channel `(available)`, not `(enabled)`.

> **Cost & time.** `anthropic` / `openai` / `codex` make **real, billable** calls;
> `claude_code` uses the local `claude` session. Each interrupt point needs only a few seconds
> of real work before you interrupt — never let a step finish. If a run looks stuck/looping
> > ~60s, interrupt it (or `stop` the session) and record it; don't let billable runs spin.

---

## 3. Set up the clone

Use a **standalone `git clone`**, not `git worktree add` — see §1. A clone's control root is
*itself*, so the §4 local-layer provider override both wins (operator-precedence) and stays
isolated; a linked worktree's local layer would resolve to the shared main checkout.

```bash
REPO=$(git rev-parse --show-toplevel)   # main checkout — report lands here, survives clone deletion
cd "$REPO"
WT=/tmp/imp-interrupt-$(date +%Y%m%d-%H%M%S)
git clone "$REPO" "$WT"                 # standalone clone of HEAD → control root resolves to $WT itself
echo "$WT"                              # your cwd for every imp run
ls "$WT"/imp/project.yaml               # the committed project marker → confirms $WT is an imp project
```

(`git clone .` copies committed state at the current branch tip. If you need uncommitted local
changes under test, commit them first or `rsync` them into `$WT` after cloning.)

You force the provider via `"$WT"/.imp/config.yaml` (the gitignored local layer) in §4.

---

## 4. Forcing a provider (local layer beats the operator's user config)

imp runs the **first** provider in the resolved `providers:` whose env_check passes. Since
`providers` is **user**-config-homed and precedence is operator-wins (`project < user <
local`), the layer that forces a provider is **local** — `$WT/.imp/config.yaml` (gitignored,
read from the control root). Because `$WT` is a standalone clone, its control root is `$WT`
itself, so this file both **wins** (highest precedence) and **stays isolated**.

Write the single-provider override (this run targets exactly one provider — `claude_code` by
default; substitute another only if the operator named it). The `remote: {driver: null}` line
keeps inherited channels off — **required in TUI mode** to prevent alt-screen corruption;
harmless in CLI mode:
```bash
mkdir -p "$WT/.imp"
cat > "$WT/.imp/config.yaml" <<'YAML'
providers: [claude_code]   # exactly one of: claude_code | anthropic | openai | codex
theme: ansi                # fixed theme so snapshots render predictably
remote:
  driver: null             # TUI mode: keep inherited channels off so the alt-screen stays clean
YAML
```

You do **not** need a `tiers:` block — tier→model bindings come from the provider **code
floor** (current models, shipped with imp). Confirm the override won — and on which layer —
before testing:
```bash
cd "$WT" && imp config                  # the `providers` row must resolve to the provider under test,
                                        # its arrow ending in `…/.imp/config.yaml (local)` — NOT `(user)`
cd "$WT" && imp config --explain high   # the high-tier model the forced provider will use
```
`imp config`'s arrow prints the leaf's **full source path** + a `(layer)` tag. If the
`providers` arrow ends in `(user)`, your file landed at the wrong path (`$WT/imp/config.yaml`,
not `$WT/.imp/config.yaml`) and the user layer silently won — fix it before recording anything.
The `--explain` line shows the floor model **before** you spend a billable call.

> Model names drift, but they live in the **code floor**, not this file. If a floor model 404s
> / "unknown model"s at launch, add a single overriding leaf to `$WT/.imp/config.yaml`
> (e.g. `tiers: {high: {openai: <current>}}`) and **note the substitution** — it's not an
> interrupt bug. codex refuses `*-codex` models, so any codex tier override must be a plain
> model (e.g. `gpt-5.5`).

---

## 5. Driving primitives (reusable procedures)

### Spawn imp — CLI mode (keys + cwd injected; `exec` so imp leads the foreground group)

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

### Spawn imp — TUI mode (`--tui`; `TERM` + `exec` both mandatory)

New:
```
mcp__impty__start(argv=["bash","-lc",
  "export TERM=xterm-256color; export ANTHROPIC_API_KEY=$(cat ~/.anthropic-key); export OPENAI_API_KEY=$(cat ~/.openai-key); exec imp new --tui"],
  cwd="<WT>", cols=100, rows=30)
```
Resume:
```
mcp__impty__start(argv=["bash","-lc",
  "export TERM=xterm-256color; export ANTHROPIC_API_KEY=$(cat ~/.anthropic-key); export OPENAI_API_KEY=$(cat ~/.openai-key); exec imp resume <id> --tui"],
  cwd="<WT>", cols=100, rows=30)
```
`TERM` is mandatory (without it the full-screen app renders wrong); `exec` is mandatory (else
bash leads the group and keys never reach imp). In TUI mode the opening intent prompt **is** the
in-app bottom buffer, so there is no separate cooked opening prompt. The `view()` header
`regime` is `screen` while the viewer is live and `scroll` after the app restored the screen.

### Reading state

- `view()` → header `[alive=… regime=… alt_screen=… cursor=(x,y)]` + grid. `alive=False` = imp
  exited; `regime=scroll` after a TUI clean pause = the alt-screen restored. Cursor parked
  right after a prompt = waiting for input.
- `view(full=True)` → the whole run so far (scrolled-off history + current screen); the
  `paused (…)` line and the change id live here.
- `snapshot()` → PNG; use it to *see* the pause line's color/glyph when judging clarity.

### **`INTERRUPT-AND-VERIFY(trigger, expected_reason)`** — the core check

1. Deliver one interrupt only.
   - **CLI mode:** keystroke `send(trigger, wait=False)` for prompt-state tests; for a
     **mid-run** Ctrl-C (points B/C/E) use `send_signal(2)` for deterministic delivery (see the
     §0 fidelity caveat).
   - **TUI mode:** keystroke `send(trigger, wait=False)` for **all** triggers, including mid-run
     Ctrl-C — the app's raw-mode bindings make keystrokes faithful.
2. `wait()` then `view()` + `view(full=True)`.
3. Assert the bar from §0, by regime + trigger:
   - **Mid-run Ctrl-C (phase-0 or materialized — disposition identical, prompt differs):** imp
     prints `⏸ paused (ctrl-c)`, stays **ATTACHED** (`alive=True`) — it does **not** exit. One
     SIGINT suffices (a second within ~1s is suppressed). The landing prompt is regime-specific:
     **phase-0** → its chat `> type your reply` prompt; **materialized** → the resteer hold
     prompt `paused — type a message to redirect, or detach to leave it paused`. To then
     **leave**, send Ctrl-D and confirm rc 130, `alive=False`, Change left paused and resumable.
   - **Mid-run Ctrl-D (both surfaces, `\x04` keystroke):** `alive=False`, rc 130, `⏸ paused
     (ctrl-d)`, Change left **paused**; `workflow_paused` `interrupt_reason == "ctrl-d"`;
     `imp resume` continues the agent (resume_target = "agent", no intervening prompt). Not in
     Backlog.
   - **Ctrl-D at the resteer hold prompt** (after a Ctrl-C): `alive=False`, rc 130, Change
     paused; `imp resume` lands back at the resteer hold prompt (resume_target = "prompt").
   - **Ctrl-C at a prompt (any regime):** neutered — clears a typed line, else `press ctrl-d
     to pause & exit`; `alive=True`, no pause.
   - In all cases: no `unclassified provider error` / `Cannot write to terminated process` /
     `paused: unknown`. **TUI mode:** also confirm `regime` went `screen` → `scroll` once the
     screen restored on the leave.
4. **Orphan check:** look for leftover *tool* subprocesses from this run —
   `bash: pgrep -af 'claude|codex|make' | grep "$WT"` — scope to `$WT` so you don't match the
   long-lived impty server, your own driver shells, or unrelated `imp`/`claude` sessions.
   Nothing tied to `$WT` should remain after the pause.
5. **Event-log check:** the Change's event log is at a deterministic path —

   ```bash
   ev="$WT/.imp/changes/<id>/events.jsonl"
   test -f "$ev" || ev=$(find "$WT" -path '*'"<id>"'*' -name 'events.jsonl' 2>/dev/null | head -1)  # fallback
   grep -o '"kind":"interrupted"' "$ev" | head     # expect a hit after an agent-phase interrupt
   grep -o '"interrupt_reason":"[a-z-]*"' "$ev" | tail  # workflow_paused trigger (ctrl-c|ctrl-d|sigterm)
   grep -o '"category":"unknown"'  "$ev" | tail     # expect NONE caused by the interrupt
   ```
   (`change_dir` is `<control_root>/.imp/changes/<id>` and `events_path` is `events.jsonl` within it. If
   you still can't locate it, fall back to `bash: cd "$WT" && imp log <id>` and judge from what
   it surfaces; note the limitation.)
6. **Record the change id** for resume + abandon. The authoritative source is
   `bash: cd "$WT" && imp list` — its first column is the change **id** (the
   `.imp/worktrees/<id>` dir name), which is what the event path and `imp resume`/`imp abandon`
   key on. The `paused` headline should print that same id; if it ever shows a slug or other
   display name instead, that's itself a bug — record it and rely on `imp list` for the id.

### **`RESUME-AND-VERIFY(id)`**

1. `start` `imp resume <id>` (§5 — add `--tui` in TUI mode). 2. Confirm it continues the *same*
Change (matching `#<handle>` / `· resuming` header), reaches a prompt or runs forward — not
`nothing to resume`, not a restart. 3. Note message clarity. 4. Assert the **resume_target**:
   - **After Ctrl-C → resteer hold prompt → Ctrl-D:** `imp resume <id>` lands back at the
     resteer hold prompt (`awaiting_user_input`) — **not** mid-agent. Expect the hold prompt to
     appear before any new agent output.
   - **After Ctrl-D mid-run:** `imp resume <id>` **continues the agent** (transcript replays,
     no intervening prompt) — no resteer hold prompt is shown; the agent picks up where it left
     off.
(The resume *banner* legitimately shows the `#<handle>` display name; only the *pause* line in
§5.6 must carry the change **id**.)

---

## 6. Test matrix

For the **single provider under test**, run the points below **for your selected surface**. Use
`INTERRUPT-AND-VERIFY` and `RESUME-AND-VERIFY`. You don't have to fire every trigger at every
point — follow the **Trigger** column. The **Mode** notes call out where CLI and TUI diverge;
everything else is shared. Keep a tally (point × trigger → outcome) for the report.

| # | Point (imp state) | How to reach it | Trigger(s) | Expected (mode-aware) |
|---|-------------------|-----------------|-----------|----------|
| **A** | Opening prompt (pre-Change) | **CLI:** `imp new`; `wait()` until parked at the first prompt. **TUI:** `imp new --tui`; the opening intent is the in-app bottom buffer | **Ctrl-C**, then in a fresh run **Ctrl-D** (CLI); **Ctrl-C** (TUI) | **Ctrl-C is neutered at the prompt** — clears a typed line, else prints `press ctrl-d to pause & exit` once; stays `alive=True`, does *not* pause. **Ctrl-D** at the empty opening prompt (no Change exists yet) → `imp: cancelled`, clean exit, no Change created. **No `interrupted` event** — nothing was running to classify. |
| **B** | Model thinking / streaming (phase-0 front-door) | Send a trivial intent (`send("add a one-line comment to the README\r")`); `wait` until output streams (no prompt) | **Ctrl-C**, **Ctrl-D** | **Ctrl-C:** `⏸ paused (ctrl-c)`, stays `alive=True` — drops to its chat `> type your reply` prompt (phase-0 regime); steer (reply), or Ctrl-D to leave. **CLI:** Ctrl-C via `send_signal(2)`. **TUI:** keystroke. **Ctrl-D (mid-run):** `\x04` keystroke both surfaces (`send("\x04", wait=False)`, no `send_signal`), rc 130, `⏸ paused (ctrl-d)`, `workflow_paused` `interrupt_reason == "ctrl-d"`, resume-to-agent. In-flight model request torn down (orphan check clean). |
| **C** | **A tool is running** | Steer the agent until it runs a shell/`make`/test tool that takes a few seconds; interrupt during it | **Ctrl-C**, **Ctrl-D** | **Ctrl-C:** `⏸ paused (ctrl-c)`, stays `alive=True`, disposition identical across phase-0 and materialized (pause, attached, steer-or-leave, never exit) — landing prompt differs: phase-0 → `> type your reply`; materialized → resteer hold prompt `paused — type a message to redirect, or detach to leave it paused`. **Ctrl-D (mid-run):** `\x04` keystroke both surfaces (`send("\x04", wait=False)`, no `send_signal`), rc 130, `⏸ paused (ctrl-d)`, suspend & detach, resume-to-agent. **The tool subprocess is killed** — orphan check must be clean. CLI Ctrl-C via `send_signal(2)`; TUI Ctrl-C via keystroke. |
| **D** | In-turn prompt (agent asks *you* mid-turn, or the reply prompt after a Ctrl-C pause) | Use an interactive step where the agent prompts you back, or the reply prompt left by a point-B/C pause | **Ctrl-C**, **ESC** | **Ctrl-C is neutered at this prompt too** — clears a typed line, else `press ctrl-d to pause & exit`; `alive=True`, a normal reply still submits, Ctrl-D leaves. **ESC is a no-op** in both modes (prompt_toolkit owns it; `alive=True`). |
| **E** | Subagent running (nested, materialized) | Drive until the workflow dispatches a subagent; interrupt while it is mid-flight | **Ctrl-C**, **Ctrl-D** | **Ctrl-C:** drops to the resteer hold prompt `paused — type a message to redirect, or detach to leave it paused` (`⏸ paused (ctrl-c)`, `alive=True`), unwinds the subagent (SubagentSuspended path), Change **not** exited. **Ctrl-D (mid-run):** `\x04` keystroke both surfaces (`send("\x04", wait=False)`, no `send_signal`), rc 130, `⏸ paused (ctrl-d)`, suspend & detach, subagent torn down (orphan check clean), resume-to-agent. CLI Ctrl-C via `send_signal(2)`; TUI Ctrl-C via keystroke. |

After the matrix: run `RESUME-AND-VERIFY` on the last paused change. Then deliver
`send_signal(sig=15)` (SIGTERM) on a fresh `imp new` run mid-execution and verify
`paused (sigterm)`.

### 6a. Negative / edge-case checks (run at least once across the suite)

These don't need to run at every point — fire each **at least once** somewhere in the suite.

- **Double / mashed Ctrl-C (point B).** With the model streaming, deliver Ctrl-C *twice in
  quick succession* — **CLI:** two `send_signal(2)` back-to-back; **TUI:** two `\x03`
  keystrokes (`send("\x03", wait=False)` twice). Expect a **single clean pause**, not a
  traceback, a double-pause, or a hang: one `⏸ paused (ctrl-c)` line, back at the interactive
  reply prompt (`alive=True`; the 2nd SIGINT within ~1s is suppressed), resumable.
- **Very-early interrupt (point A window).** Ctrl-C in the brief window right after spawning
  imp, *before* the first prompt/agent appears — **CLI:** `send("\x03", wait=False)` immediately
  after the §5 spawn without waiting for the prompt; **TUI:** the same keystroke before the
  opening buffer is ready. Nothing is running, so expect **no `interrupted` event**. Record
  whatever happens: clean handling is preferred (a neutered Ctrl-C hint, or a clean
  cancel/exit); a traceback, a hang, or an unkillable process is a bug.

---

## 7. Report

Write a report to the **main checkout** (not the clone):

```
"$REPO"/docs/bugs/<YYYY-MM-DD>-interrupt-playbook-results.md
```

For each point and trigger, record: the state reached, the trigger delivered, the full
`view(full=True)` snippet (or `snapshot()` path), whether the §0 bar passed, and any
deviation from expected behavior.

Include:
- **Build under test:** `git -C "$REPO" rev-parse --short HEAD`
- **Surface:** CLI or TUI
- **Provider:** which one and which tier/model resolved (`imp config --explain high`)
- **Tally:** a per-point × per-trigger result table (PASS / FAIL / SKIP + one-line note)
- **Defects:** for each failure, the exact sequence of events, the expected vs actual behavior,
  and a reproduction recipe

A **clean run still produces a report** — write "no defects observed" explicitly. A passing run
with no artifact is the most likely failure of this convention.

---

## 8. Cleanup and wrap-up

```bash
cd "$WT" && imp abandon <id>   # abandon any open Changes in the clone (get <id> from §5.6 / imp list)
rm -rf "$WT"
git -C "$REPO" status --short  # only the report file should be new
```

If anything in the main checkout is different from the report file, flag it.

Stop with a one-paragraph summary: build tested, surface and provider, tally of PASS/FAIL/SKIP
per point, headline defects (or "none"), report path.

---

## 9. Quick reference

| Interrupt | `send` | Out-of-band | What fires |
|-----------|--------|-------------|------------|
| **Ctrl-C** (at prompt) | `"\x03"` | — | intercepted — clears the line / `press ctrl-d to pause & exit` hint; never pauses |
| **Ctrl-C** (mid-run, CLI) | unreliable | `send_signal(2)` | `SIGINT` → coordinator → pause, stay attached (`alive=True`), never exit; landing prompt: phase-0 `> type your reply` / materialized resteer hold prompt |
| **Ctrl-C** (mid-run, TUI) | `"\x03"` | — | app binding → coordinator → pause, stay attached (`alive=True`), never exit; landing prompt: phase-0 `> type your reply` / materialized resteer hold prompt |
| **Ctrl-D** (at prompt / resteer hold) | `"\x04"` | — | `EOFError` → phase-0 leave (`cancelled` / Backlog / archive); **materialized at resteer hold → detach** (rc 130, resume-to-prompt) |
| **Ctrl-D** (mid-run, both surfaces) | `"\x04"` | — | cbreak `/dev/tty` reader reads byte directly — no `send_signal` needed; suspend & detach (rc 130, `interrupt_reason == "ctrl-d"`, resume-to-agent) |
| **SIGTERM** | — | `send_signal(15)` | coordinator |
