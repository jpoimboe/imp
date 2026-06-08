# imp interrupt playbook — Ctrl-C / ESC / Ctrl-D acceptance test (impty MCP)

> **You are the test driver.** This file is your full instruction set. You drive `imp`
> *through the impty MCP server* (`mcp__impty__start` / `send` / `view` / `wait` /
> `transcript` / `snapshot` / `stop`) — a real pty with a faithful pyte render — fire
> interrupts (Ctrl-C, **ESC**, Ctrl-D) at well-defined points across four providers and
> several agent states (model thinking, **a tool running**, an in-turn prompt, a
> subagent, resume), judge whether imp behaves and *speaks* correctly, and write every
> defect to a bug report. Work in a throwaway standalone `git clone` and abandon everything
> at the end.
>
> This is the acceptance test for the **esc-and-clean-interrupt** feature
> (`imp/specs/2026-06-04-esc-and-clean-interrupt.md`), which is **on `master`**. The
> "expected" behavior below is that feature's target. If you somehow run an older build
> that predates it, the ESC / pause-reason / clean-classification checks are *expected* to
> fail — record them as to-dos, not surprises.
>
> **Scope: four providers.** The feature (and so the §0 "clean classification" bar)
> covers `claude_code`, `anthropic`, `openai`, and `codex`.
>
> Read the whole file before you touch a tool.

---

## 0. The three interrupt triggers and how impty delivers them

impty claims its pty slave as the child's controlling terminal, so keystrokes you `send`
behave exactly as on a physical terminal. The MCP server exposes **no** out-of-band
signal tool — every interrupt is a keystroke through `send`. Fire each with `wait=False`
so it goes immediately, then read with `wait` / `view` / `transcript` / `snapshot`.

| Trigger | `send` data | Path through imp |
|---------|-------------|------------------|
| **Ctrl-C** | `"\x03"` | Raw byte at a prompt → prompt_toolkit `KeyboardInterrupt`. Real SIGINT while an agent runs (cooked mode) → `anyio.open_signal_receiver` → `InterruptCoordinator.signal_pause("ctrl-c")`. |
| **ESC** | `"\x1b"` | A bare ESC byte. While an agent is *thinking* (no prompt active) the `EscKeyReader` on `/dev/tty` catches it → `InterruptCoordinator.signal_pause("esc")`. At a prompt_toolkit prompt it is **not** intercepted (emacs meta-prefix / vi exit) — see the negative test in §6. |
| **Ctrl-D** | `"\x04"` | EOF at a prompt → `EOFError` → `pause_and_exit(reason="ctrl-d")`. (Only meaningful at a prompt.) |

> `SIGTERM` → `paused (sigterm)` is also part of the feature but cannot be delivered as a
> keystroke. Optionally test it out-of-band with `bash: kill -TERM <pid>`. The pid is not
> exposed in the impty status header; get it with `pgrep -f 'imp (new|resume)'` in the
> clone. If awkward, **skip it and note the skip** — it is not the focus here.

### The bar for "correct" (every interrupt point, unless stated otherwise)

A correct imp does **all** of:

- **Stops promptly on one interrupt** — no mashing, no multi-second hang.
- **Pauses cleanly and exits** — process ends (`alive=False` in the impty status header),
  exit code **130**, with a clear final line whose reason matches the trigger:
  - Ctrl-C → `⏸ Change <id>: paused (ctrl-c)`
  - ESC → `⏸ Change <id>: paused (esc)`
  - Ctrl-D → `⏸ Change <id>: paused (ctrl-d)`
  - (`⏸` shows only with color; otherwise the same text without the glyph.)
- **No ugly internals leak.** The strings `unclassified provider error` and
  `Cannot write to terminated process` and a `paused: unknown (CLIConnectionError)`
  headline must **not** appear. The interrupt is a clean, expected stop — not an error.
- **The event log records it as an interrupt, not an unknown error.** Two distinct records
  are written on an agent-phase interrupt — don't confuse them:
  - a **`workflow_paused`** event carrying `payload.reason` (`ctrl-c|esc|ctrl-d|sigterm`) —
    written by the pause itself;
  - an **`error`** event with `payload.category == "config"` and
    `payload.kind == "interrupted"`, never `category == "unknown"` — written by the
    provider runner when it was mid-flight.

  §5's greps target the **`error`/`interrupted`** record (the classification check). It
  only exists when a runner was actually running (so not at point A — see §6).
- **No orphaned subprocesses** — after interrupting a running tool, no leftover
  `claude` / `codex` / `bash` / `make` / model-transport process keeps running.
- **Is resumable** — `imp resume <id>` continues the Change (same id), reaching a
  prompt or running forward; never `nothing to resume`, never a from-scratch restart.
- **Speaks clearly to the user.** Judge the human-visible text with fresh eyes; an
  unclear, missing, or misleading message **is a bug** even when the exit code is right.

> The interrupt **decision is Pause, not Cancel** (per the spec's "Out of scope"). imp
> does *not* cancel just the current turn and drop you back at its live prompt — it pauses
> the whole run and exits, resumable. If any provider/point does something else, record
> exactly what it did.

---

## 1. Assumptions (correct me before running if any are wrong)

- **Which `imp`:** the installed `imp` on PATH (a current build off `master`, which carries
  the feature). State the commit you tested in the report.
- **Where:** a fresh standalone `git clone` of this repo (created in §3) — **not** a
  `git worktree`. A linked worktree shares the main checkout's git-common-dir, and imp's
  `find_control_root` resolves any linked worktree back to the **main checkout** (`git
  rev-parse --git-common-dir` → parent), so imp would read the *main* `imp/config.yaml`
  and the per-provider rewrite in §4 would silently no-op. A standalone clone's control
  root resolves to *itself*, so `$WT/imp/config.yaml` is honored. Every `imp new` / `imp
  resume` runs with cwd inside the clone, so all Changes (branches, nested worktrees,
  journals) are contained and discardable.
- **Providers:** `claude_code`, `anthropic`, `openai`, `codex` — all four are in scope
  for the full §0 classification bar.
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
```

Provider gating (mirrors imp's `_env_check`):

| Provider      | Needs |
|---------------|-------|
| `claude_code` | `claude` on PATH (no key) |
| `anthropic`   | `ANTHROPIC_API_KEY` (from `~/.anthropic-key`) |
| `openai`      | `OPENAI_API_KEY` (from `~/.openai-key`) |
| `codex`       | `codex` on PATH **and** the codex SDK importable in imp's venv **and** `~/.codex/auth.json` |

For any provider missing a prerequisite: **skip and note it** — not a bug. imp's own
startup env_check is the source of truth: if imp skips or errors a provider at launch,
treat that as the skip signal and note it.

> **Cost & time.** `anthropic` / `openai` / `codex` make **real, billable** calls;
> `claude_code` uses the local `claude` session. Each interrupt point needs only a few
> seconds of real work before you interrupt — never let a step finish. If a run looks
> stuck/looping > ~60s, interrupt it (or `stop` the session) and record it; don't let
> billable runs spin.

---

## 3. Set up the clone

Use a **standalone `git clone`**, not `git worktree add` — see §1 (a linked worktree's
config is ignored because `find_control_root` resolves it to the main checkout). A clone's
control root is itself, so the §4 config-forcing actually takes effect.

```bash
cd /home/jpoimboe/imp
WT=/tmp/imp-interrupt-$(date +%Y%m%d-%H%M%S)
git clone . "$WT"                       # standalone clone of HEAD → control root resolves to $WT itself
echo "$WT"                              # your cwd for every imp run
sed -n '1,12p' "$WT"/imp/config.yaml    # confirm this is the live project config (providers/tiers/theme)
```

(`git clone .` copies committed state at the current branch tip. If you need uncommitted
local changes under test, commit them first or `rsync` them into `$WT` after cloning.)

You rewrite `"$WT"/imp/config.yaml` per provider (§4).

---

## 4. Forcing a provider

imp uses the **first** provider in `providers:` whose env_check passes. Overwrite
`"$WT"/imp/config.yaml` with the single-provider block for the one under test:

**claude_code**
```yaml
providers: [claude_code]
tiers: {low: {claude_code: haiku}, medium: {claude_code: sonnet}, high: {claude_code: opus}}
theme: ansi
```
**anthropic**
```yaml
providers: [anthropic]
tiers: {low: {anthropic: haiku}, medium: {anthropic: sonnet}, high: {anthropic: opus}}
theme: ansi
```
**openai**
```yaml
providers: [openai]
tiers: {low: {openai: gpt-5.4-mini}, medium: {openai: gpt-5.3-codex}, high: {openai: gpt-5.5}}
theme: ansi
```
**codex** (codex refuses `*-codex` models → all tiers `gpt-5.5`)
```yaml
providers: [codex]
tiers: {low: {codex: gpt-5.5}, medium: {codex: gpt-5.5}, high: {codex: gpt-5.5}}
theme: ansi
```

> Model names drift. If a tier model 404s / "unknown model"s at launch, the available
> models changed — adjust the tier to a current one and **note the substitution**; it's
> not an interrupt bug.

---

## 5. Driving primitives (reusable procedures)

### Spawn imp (keys + cwd injected; `exec` so imp leads the foreground group)

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
`exec` is required — without it bash, not imp, leads the foreground group and `\x03`/ESC
would not reach imp.

### Reading state

- `view()` → header `[alive=… alt_screen=… cursor=(x,y)]` + grid. `alive=False` = imp
  exited. Cursor parked right after a prompt = waiting for input.
- `transcript()` → full scroll; the `paused (…)` line and the change id live here.
- `snapshot()` → PNG; use it to *see* the pause line's color/glyph when judging clarity.

### **`INTERRUPT-AND-VERIFY(trigger, expected_reason)`** — the core check

1. `send(trigger, wait=False)` (one interrupt only).
2. `wait()` then `view()` + `transcript()`.
3. Assert the bar from §0: `alive=False`; final line `paused (<expected_reason>)`; no
   `unclassified provider error` / `Cannot write to terminated process` /
   `paused: unknown`; one interrupt sufficed.
4. **Orphan check:** look for leftover *tool* subprocesses from this run —
   `bash: pgrep -af 'claude|codex|make' | grep "$WT"` — scope to `$WT` so you don't match
   the long-lived impty server, your own driver shells, or unrelated `imp`/`claude`
   sessions. Nothing tied to `$WT` should remain after the pause.
5. **Event-log check:** the Change's event log is at a deterministic path —

   ```bash
   ev="$WT/.imp/worktrees/<id>/.imp/change/events.jsonl"
   test -f "$ev" || ev=$(find "$WT" -path '*'"<id>"'*' -name 'events.jsonl' 2>/dev/null | head -1)  # fallback
   grep -o '"kind":"interrupted"' "$ev" | head     # expect a hit after an agent-phase interrupt
   grep -o '"category":"unknown"'  "$ev" | tail     # expect NONE caused by the interrupt
   ```
   (`change_dir` is `<worktree>/.imp/change` and `events_path` is `events.jsonl` within it;
   `<worktree>` is `$WT/.imp/worktrees/<id>` because cwd is `$WT`. If you still can't
   locate it, fall back to `bash: cd "$WT" && imp log <id>` and judge from what it
   surfaces; note the limitation.)
6. **Record the change id** for resume + abandon. The authoritative source is
   `bash: cd "$WT" && imp status` — its first column is the change **id** (the
   `.imp/worktrees/<id>` dir name), which is what the event path and `imp resume`/`imp
   abandon` key on. The `paused` headline should print that same id; if it ever shows a
   slug or other display name instead, that's itself a bug (a slug is never a ref) — record
   it and rely on `imp status` for the id.

### **`RESUME-AND-VERIFY(id)`**

1. `start` `imp resume <id>` (§5). 2. Confirm it continues the *same* Change (matching
`#<slug>` / `· resuming` header), reaches a prompt or runs forward — not `nothing to
resume`, not a restart. 3. Note message clarity.

---

## 6. Test matrix

For **each** in-scope provider, run the points below. Use `INTERRUPT-AND-VERIFY` and
`RESUME-AND-VERIFY`. You don't have to fire every trigger at every point — follow the
**Trigger** column, which targets the meaningful combinations. Keep a tally
(provider × point × trigger → outcome) for the report.

| # | Point (imp state) | How to reach it | Trigger(s) | Expected |
|---|-------------------|-----------------|-----------|----------|
| **A** | Opening interactive prompt (raw mode) | `imp new`; `wait()` until parked at the first prompt; type nothing | **Ctrl-C**, then in a fresh run **Ctrl-D** | A1: `paused (ctrl-c)`. A2: `paused (ctrl-d)`. **No `interrupted` event is expected here** — nothing was running to classify; the event-log check does not apply at A. The bar that *does* apply: clean `paused` line, `alive=False`, exit 130, resumable. |
| **B** | Model thinking / streaming (cooked mode) | At the prompt, send a trivial intent (`send("add a one-line comment to the README\r")`); `wait` until you *see* output streaming (`view` shows no prompt) | **Ctrl-C** in one run; **ESC** in another | `paused (ctrl-c)` / `paused (esc)`. In-flight model request torn down (orphan check). |
| **C** | **A tool is running** (subprocess executing) | Drive the agent until it runs a tool that takes a few seconds — e.g. a shell/`make`/test tool. If the default workflow reaches a build/test step, interrupt during it; otherwise steer the intent toward something that triggers a shell command, and watch `view`/`transcript` for tool activity | **Ctrl-C** in one run; **ESC** in another | `paused (ctrl-c)` / `paused (esc)`. **The tool subprocess is killed** — orphan check must be clean (this is `runner.interrupt()` → `terminate_running_processes`). |
| **D** | In-turn user-chat prompt (interactive agent asks *you* mid-turn) | Use an interactive step (e.g. the spec/open-by-asking turn) where the agent prompts you back; wait until parked at *that* prompt | **Ctrl-C**; separately **ESC** | Ctrl-C → `paused (ctrl-c)`. **ESC at this prompt is the negative test:** ESC must NOT pause — prompt_toolkit consumes it (emacs meta / no-op); imp stays at the prompt, `alive=True`. Verify by sending `\x1b` then `view()` — still at prompt — then a normal reply still submits. |
| **E** | Subagent running (nested) | Drive until the workflow dispatches a subagent (a sub-step doing work); interrupt while the subagent is mid-flight | **Ctrl-C** or **ESC** | Pauses the whole run cleanly (`paused (…)`), unwinds the subagent (SubagentSuspended path), resumable. Orphan check clean. |
| **F** | During resume | `RESUME-AND-VERIFY` a paused Change, then interrupt again at whichever of A/B/C you didn't already stress for this provider | **Ctrl-C** or **ESC** | Same bar; the *second* pause is still resumable (pause→resume is idempotent across cycles). |

### Negative / clarity checks (run at least once across the suite)

- **ESC at the opening prompt (A):** like D's negative test — ESC must be prompt_toolkit's,
  not a pause. `alive=True` after `\x1b`; a normal reply still submits.
- **Double / mashed Ctrl-C:** at point B, send `\x03` twice quickly. Expect a single clean
  pause, not a traceback, double-pause, or hang.
- **Very-early interrupt:** Ctrl-C in the brief window right after `imp new` before the
  first prompt/agent appears. Record whatever happens (clean pause preferred; a crash or
  unkillable state is a bug).
- **Clean-resume positive control:** for ≥1 Change per provider, after a pause do a clean
  `RESUME-AND-VERIFY` and let it reach a prompt *without* interrupting — proves imp truly
  recovered state, not just exited tidily.

---

## 7. Bug report

Write to the **main checkout** (survives clone deletion), in the bugs dir
(`mkdir -p` it if absent):

```
/home/jpoimboe/imp/docs/bugs/<YYYY-MM-DD>-interrupt-playbook-results.md
```

Include:

1. **Build under test** — which `imp` (branch/commit:
   `git -C /home/jpoimboe/imp rev-parse --short HEAD`, plus whether the
   esc-and-clean-interrupt feature is present) and the impty commit.
2. **Summary matrix** — rows = providers; columns = A1/A2 / B-ctrlc / B-esc / C-ctrlc /
   C-esc / D-ctrlc / D-esc(neg) / E / F / clean-resume; cells = ✅ / ⚠️ issue / ⏭️ skipped
   (one-line reason).
3. **One section per bug** — title + severity (blocker/major/minor/cosmetic); provider &
   point & trigger; **expected** (cite §0); **what happened** (paste `transcript()`
   excerpt and/or a `snapshot()` PNG path; include `alive`/exit, whether one interrupt
   sufficed, orphan-check result, event-log classification); **repro** (exact `start`
   argv, keystrokes/timing, config); **notes**.
4. **Message-clarity findings** — any user-facing text that was unclear, missing, or
   misleading even where the exit code was right (e.g. wrong/empty pause reason, leaked
   internals, no resume hint).
5. **Skips** — providers/points/triggers skipped and why.

Zero bugs is still a report: write the matrix and an explicit "no defects observed" note
per provider — a clean run is a recorded result.

---

## 8. Cleanup — abandon everything

```bash
cd "$WT"
imp log                      # or: imp status — enumerate change ids (optional; the clone is discarded wholesale)
imp abandon <id>             # optional: exercise abandon inside the clone; the rm below discards everything regardless

cd /home/jpoimboe/imp
rm -rf "$WT"                 # the standalone clone holds every branch / nested worktree / journal — deleting it removes all churn
git -C /home/jpoimboe/imp status --short   # only the report file should be new
```

If anything in the **main** checkout changed besides the report, flag it — the clone
was supposed to contain all the churn.

---

## 9. Done

Report a one-paragraph wrap-up: build tested (commit + whether the feature is present),
which providers/points/triggers ran, the headline findings (especially anything failing
the bar in §0), the report path, and confirmation the clone and all test Changes were
abandoned. Then stop.
