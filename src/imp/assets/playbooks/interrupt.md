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
> This is the acceptance test for the **esc-and-clean-interrupt** feature
> (`imp/specs/2026-06-04-esc-and-clean-interrupt.md`); the TUI mode also covers the `--tui`
> interrupt bindings from **tui-run-viewer** (`imp/specs/2026-06-11-tui-run-viewer.md`). Both
> are **on `master`**. The "expected" behavior below is those features' target. If you somehow
> run an older build that predates them, the pause-reason / clean-classification (and
> `--tui`) checks are *expected* to fail — record them as to-dos, not surprises.
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

| Trigger | `send` data | Path through imp |
|---------|-------------|------------------|
| **Ctrl-C** | `"\x03"` | Raw byte at a prompt → prompt_toolkit `KeyboardInterrupt`. Real SIGINT while an agent runs (CLI/cooked mode) → `anyio.open_signal_receiver` → `InterruptCoordinator.signal_pause("ctrl-c")`. In TUI mode the app's key binding drives the same coordinator. |
| **Ctrl-D** | `"\x04"` | EOF at a prompt → `EOFError` → `pause_and_exit(reason="ctrl-d")`. (Only meaningful at a prompt; CLI mode.) |

> **ESC is a no-op.** A bare ESC byte (`"\x1b"`) never pauses imp: at a prompt prompt_toolkit
> owns it (emacs meta-prefix / vi exit), and while an agent is thinking it is ignored. Do not
> expect a `paused (esc)` line anywhere — if ESC ever pauses a run, that is a regression.

> `SIGTERM` → `paused (sigterm)` is also part of the feature. Deliver it in one call with
> `send_signal(sig=15)` — it signals imp's process group directly, no pid scraping. Run it
> once where convenient during this run; it is no longer a skippable afterthought.

### 0c. The bar for "correct" (every interrupt point, unless stated otherwise)

A correct imp does **all** of:

- **Stops promptly on one interrupt** — no mashing, no multi-second hang.
- **Pauses cleanly and exits** — process ends (`alive=False` in the impty status header),
  exit code **130**, with a clear final line whose reason matches the trigger:
  - Ctrl-C → `⏸ Change <id>: paused (ctrl-c)`
  - Ctrl-D → `⏸ Change <id>: paused (ctrl-d)`
  - (`⏸` shows only with color; otherwise the same text without the glyph.)
  - In **TUI mode** the alt-screen also restores on the clean pause: `regime` goes
    `screen` → `scroll`, and the `paused (…)` line lands in normal scrollback.
- **No ugly internals leak.** The strings `unclassified provider error` and
  `Cannot write to terminated process` and a `paused: unknown (CLIConnectionError)` headline
  must **not** appear. The interrupt is a clean, expected stop — not an error.
- **The event log records it as an interrupt, not an unknown error.** Two distinct records
  are written on an agent-phase interrupt — don't confuse them:
  - a **`workflow_paused`** event carrying `payload.reason` (`ctrl-c|ctrl-d|sigterm`) —
    written by the pause itself;
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

> The interrupt **decision is Pause, not Cancel** (per the spec's "Out of scope"). imp does
> *not* cancel just the current turn and drop you back at its live prompt — it pauses the whole
> run and exits, resumable. If any provider/point does something else, record exactly what it
> did.

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
3. Assert the bar from §0: `alive=False`; final line `paused (<expected_reason>)`; no
   `unclassified provider error` / `Cannot write to terminated process` / `paused: unknown`;
   one interrupt sufficed. **TUI mode:** also confirm `regime` went `screen` → `scroll` (the
   alt-screen restored on the clean pause).
4. **Orphan check:** look for leftover *tool* subprocesses from this run —
   `bash: pgrep -af 'claude|codex|make' | grep "$WT"` — scope to `$WT` so you don't match the
   long-lived impty server, your own driver shells, or unrelated `imp`/`claude` sessions.
   Nothing tied to `$WT` should remain after the pause.
5. **Event-log check:** the Change's event log is at a deterministic path —

   ```bash
   ev="$WT/.imp/changes/<id>/events.jsonl"
   test -f "$ev" || ev=$(find "$WT" -path '*'"<id>"'*' -name 'events.jsonl' 2>/dev/null | head -1)  # fallback
   grep -o '"kind":"interrupted"' "$ev" | head     # expect a hit after an agent-phase interrupt
   grep -o '"category":"unknown"'  "$ev" | tail     # expect NONE caused by the interrupt
   ```
   (`change_dir` is `<control_root>/.imp/changes/<id>` and `events_path` is `events.jsonl` within it. If
   you still can't locate it, fall back to `bash: cd "$WT" && imp log <id>` and judge from what
   it surfaces; note the limitation.)
6. **Record the change id** for resume + abandon. The authoritative source is
   `bash: cd "$WT" && imp ls` — its first column is the change **id** (the
   `.imp/worktrees/<id>` dir name), which is what the event path and `imp resume`/`imp abandon`
   key on. The `paused` headline should print that same id; if it ever shows a slug or other
   display name instead, that's itself a bug — record it and rely on `imp ls` for the id.

### **`RESUME-AND-VERIFY(id)`**

1. `start` `imp resume <id>` (§5 — add `--tui` in TUI mode). 2. Confirm it continues the *same*
Change (matching `#<handle>` / `· resuming` header), reaches a prompt or runs forward — not
`nothing to resume`, not a restart. 3. Note message clarity. (The resume *banner* legitimately
shows the `#<handle>` display name; only the *pause* line in §5.6 must carry the change **id**.)

---

## 6. Test matrix

For the **single provider under test**, run the points below **for your selected surface**. Use
`INTERRUPT-AND-VERIFY` and `RESUME-AND-VERIFY`. You don't have to fire every trigger at every
point — follow the **Trigger** column. The **Mode** notes call out where CLI and TUI diverge;
everything else is shared. Keep a tally (point × trigger → outcome) for the report.

| # | Point (imp state) | How to reach it | Trigger(s) | Expected (mode-aware) |
|---|-------------------|-----------------|-----------|----------|
| **A** | Opening prompt | **CLI:** `imp new`; `wait()` until parked at the first prompt. **TUI:** `imp new --tui`; the opening intent is the in-app bottom buffer | **Ctrl-C**, then in a fresh run **Ctrl-D** (CLI); **Ctrl-C** (TUI) | `paused (ctrl-c)`; CLI also A2 `paused (ctrl-d)`. **No `interrupted` event here** — nothing was running to classify. Clean `paused` line, `alive=False`, exit 130, resumable. |
| **B** | Model thinking / streaming | Send a trivial intent (`send("add a one-line comment to the README\r")`); `wait` until output streams (no prompt) | **Ctrl-C** | `paused (ctrl-c)`. **CLI:** Ctrl-C via `send_signal(2)`. **TUI:** keystroke. In-flight model request torn down (orphan check). |
| **C** | **A tool is running** | Steer the agent until it runs a shell/`make`/test tool that takes a few seconds; interrupt during it | **Ctrl-C** | `paused (ctrl-c)`. **The tool subprocess is killed** — orphan check must be clean. CLI Ctrl-C via `send_signal(2)`; TUI via keystroke. |
| **D** | In-turn user-chat prompt (agent asks *you* mid-turn) | Use an interactive step where the agent prompts you back; wait until parked at *that* prompt | **Ctrl-C** | Ctrl-C → `paused (ctrl-c)`. **ESC at this prompt is a no-op** in both modes (prompt_toolkit owns it; `alive=True`, a normal reply still submits). |
| **E** | Subagent running (nested) | Drive until the workflow dispatches a subagent; interrupt while it is mid-flight | **Ctrl-C** | Pauses the whole run cleanly (`paused (…)`), unwinds the subagent (SubagentSuspended path), resumable. Orphan check clean. CLI Ctrl-C via `send_signal(2)`; TUI via keystroke. |

After the matrix: run `RESUME-AND-VERIFY` on the last paused change. Then deliver
`send_signal(sig=15)` (SIGTERM) on a fresh `imp new` run mid-execution and verify
`paused (sigterm)`.

### 6a. Negative / edge-case checks (run at least once across the suite)

These don't need to run at every point — fire each **at least once** somewhere in the suite.

- **Double / mashed Ctrl-C (point B).** With the model streaming, deliver Ctrl-C *twice in
  quick succession* — **CLI:** two `send_signal(2)` back-to-back; **TUI:** two `\x03`
  keystrokes (`send("\x03", wait=False)` twice). Expect a **single clean pause**, not a
  traceback, a double-pause, or a hang: one `paused (ctrl-c)` line, `alive=False`, resumable.
- **Very-early interrupt (point A window).** Ctrl-C in the brief window right after spawning
  imp, *before* the first prompt/agent appears — **CLI:** `send("\x03", wait=False)` immediately
  after the §5 spawn without waiting for the prompt; **TUI:** the same keystroke before the
  opening buffer is ready. Nothing is running, so expect **no `interrupted` event**. Record
  whatever happens: a clean `paused`/exit is preferred; a traceback, a hang, or an unkillable
  process is a bug.

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
cd "$WT" && imp abandon <id>   # abandon any open Changes in the clone (get <id> from §5.6 / imp ls)
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
| **Ctrl-C** (at prompt) | `"\x03"` | — | prompt_toolkit `KeyboardInterrupt` |
| **Ctrl-C** (mid-run, CLI) | unreliable | `send_signal(2)` | `SIGINT` → coordinator |
| **Ctrl-C** (mid-run, TUI) | `"\x03"` | — | app binding → coordinator |
| **Ctrl-D** (at prompt) | `"\x04"` | — | `EOFError` → `pause_and_exit` |
| **SIGTERM** | — | `send_signal(15)` | coordinator |
