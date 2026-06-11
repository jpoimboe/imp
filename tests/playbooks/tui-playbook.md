# imp TUI run-viewer playbook — `--tui` acceptance test (impty MCP)

> **You are the test driver.** This file is your full instruction set. You drive `imp --tui`
> *through the impty MCP server* (`mcp__impty__start` / `send` / `send_signal` / `view` /
> `wait` / `snapshot` / `stop`) — a real pty with a faithful pyte render that captures the
> **alternate screen** — walk a full `imp new --tui` / `imp resume --tui` cycle on a real
> feature-sized intent, exercise every key binding and the in-app prompt, judge imp against
> the 11 acceptance criteria, and write every defect to a results report. Work in a throwaway
> standalone `git clone` and abandon everything at the end.
>
> This is the acceptance test for the **tui-run-viewer** feature
> (`imp/specs/2026-06-11-tui-run-viewer.md`), which is **on `master`**. The "expected"
> behavior below is that feature's target; the design rationale is in
> `docs/superpowers/specs/2026-06-11-tui-run-viewer-design.md`. If you run an older build that
> predates it, the `--tui` flag / alt-screen / Ctrl-O / in-app-prompt checks are *expected* to
> fail — record them as to-dos, not surprises.
>
> **Provider: `claude_code`** (the local `claude` session — no API key, not billable). The TUI
> surface is provider-independent, so one provider is enough; swap if `claude_code` is
> unavailable.
>
> Read the whole file before you touch a tool.

---

## 0. Why impty, and the alt-screen / keystroke fidelity notes

`--tui` is a `full_screen=True` prompt_toolkit `Application` on the **alternate screen** —
imp's first use of it. impty claims its pty slave as imp's controlling terminal and renders
the alt-screen faithfully:

- `view()`'s status header reports **`regime`**: `screen` = the alt-screen is up (a run is
  live in the viewer); `scroll` = normal scrollback (before launch, or after the app restored
  the screen on exit). This is your primary signal for "in the TUI" vs "exited".
- `snapshot()` → PNG. Use it to **see** the expanded tree, status bar, colors, glyphs, and
  the bottom prompt buffer — the grid `view()` returns can't show color/weight.

> **TERM must be set.** Without `TERM` prompt_toolkit degrades and the full-screen app renders
> wrong (spec's "impty harness must set TERM"). Always spawn with `TERM=xterm-256color`
> exported (the `bash -lc` spawn in §4 does this).

> **Keystrokes are the faithful path here — unlike the interrupt-playbook.** In `--tui` mode
> the full-screen app owns the tty in **raw mode** and binds the keys itself
> (`PageUp/PageDown`, `End`, `Ctrl-O`, **`ESC`**, **`Ctrl-C`**) — there is **no** `EscKeyReader`
> and **no** cooked-mode signal receiver in the loop (`InterruptCoordinator` is driven by the
> app's key handlers). So `send("\x1b")` / `send("\x03")` are delivered to prompt_toolkit as
> keys and are the correct test, *not* `send_signal`. Two caveats: (a) a bare `ESC` is held by
> prompt_toolkit's escape-flush timeout (~0.5s) before it fires — `send("\x1b")` then `wait()`;
> (b) impty can still drop the odd keystroke — if a result looks wrong, re-send once and, only
> if it persists, record it (note the suspected harness artifact).

### Key bytes you will send

| Key | `send` data | Binding under test |
|-----|-------------|--------------------|
| PageUp | `"\x1b[5~"` | scroll up, disengage auto-follow (crit 3) |
| PageDown | `"\x1b[6~"` | scroll down (crit 3) |
| End | `"\x1b[F"` (try `"\x1bOF"` / `"\x1b[4~"` if inert) | re-engage auto-follow / jump to tail (crit 3) |
| Ctrl-O | `"\x0f"` | toggle verbosity expanded↔lean (crit 4) |
| ESC | `"\x1b"` | **pause** `signal_pause("esc")` — not quit (crit 5) |
| Ctrl-C | `"\x03"` | interrupt via same `InterruptCoordinator` (crit 5) |
| Enter | `"\r"` | submit the bottom prompt buffer |
| Tab | `"\t"` | `/command` / skill completion (crit 6) |
| Ctrl-U | `"\x15"` | clear the prompt line |

---

## 1. Assumptions (correct me before running if any are wrong)

- **Which `imp`:** the installed `imp` on PATH (a current build off `master`, which carries the
  feature). State the commit you tested: `git -C "$REPO" rev-parse --short HEAD`.
- **Where:** a fresh standalone `git clone` (created in §3) — **not** a `git worktree`. A
  clone's control root is *itself*, so `$WT/.imp/config.yaml` both wins and stays isolated; a
  linked worktree's local layer would resolve to — and pollute — the shared main checkout.
  Every `imp` run uses cwd inside the clone, so all Changes (branches, nested worktrees,
  journals) are contained and discardable.
- **Provider:** `claude_code` — `claude` on PATH, local session, **no key, not billable**.
- **impty MCP server** is running from `.mcp.json` with controlling-terminal support.
- **Results report** goes to the **main checkout** (survives clone deletion). Path in §7.

If any of these is wrong, say so and stop.

---

## 2. Preflight (skip-with-a-note, don't fail)

```bash
which imp claude
imp new -h | grep -q -- --tui && echo "tui-flag present" || echo "NO --tui (old build)"
```
(`--tui` is a subcommand flag — it shows under `imp new -h` / `imp resume -h`, **not** the
top-level `imp --help`.)

If `claude` is missing, swap to a provider you have (`anthropic`/`openai` need their key
exported — see the interrupt-playbook's §5 spawn — and are billable; keep each run short).
If `--tui` is absent, you're on a pre-feature build — record the whole suite as expected-fail.

> **Cost & time.** `claude_code` is local and free; let a run reach a couple of real steps so
> the expanded tree, scroll, and Ctrl-O have content to act on. If a run looks stuck/looping
> > ~90s, ESC-pause it (or `stop` the session) and record it — don't let it spin.

---

## 3. Set up the clone

```bash
REPO=$(git rev-parse --show-toplevel)   # main checkout — report lands here, survives clone deletion
cd "$REPO"
WT=/tmp/imp-tui-$(date +%Y%m%d-%H%M%S)
git clone "$REPO" "$WT"                 # standalone clone of HEAD → control root resolves to $WT itself
echo "$WT"                              # your cwd for every imp run
mkdir -p "$WT/.imp"
cat > "$WT/.imp/config.yaml" <<'YAML'
providers: [claude_code]       # local provider; no key
theme: dark                    # primary pass; you will also sweep light + ansi (§6-L)
YAML
cd "$WT" && imp config          # confirm `providers` resolves to claude_code on the `(local)` layer
```

---

## 4. Driving primitives

Spawn imp through `bash -lc` **with `exec`** so imp (not bash) leads the foreground group and
your keystrokes/ESC/Ctrl-C reach it, and so it inherits the full login env `claude_code` needs:

```
mcp__impty__start(argv=["bash","-lc","export TERM=xterm-256color; exec imp new --tui"],
                  cwd="<WT>", cols=100, rows=30)
mcp__impty__start(argv=["bash","-lc","export TERM=xterm-256color; exec imp resume <id> --tui"],
                  cwd="<WT>", cols=100, rows=30)
```
`exec` is **mandatory** — without it bash leads the foreground group and `\x1b`/`\x03` never
reach imp. For a theme sweep, prepend `export IMP_THEME=light;` (or `ansi`) before `exec`.

Read state: `view()` (header `regime`/`cursor` + grid), `view(full=True)` (scrollback + screen
— where the `paused (…)` line and collapsed transcript land after exit), `snapshot()` (PNG).
Act: `send(<bytes>)` (default `wait=True` type-then-look), `wait()` to settle, `stop()` to end.

> The opening intent prompt **is** the in-app bottom buffer: `imp new --tui` launches the
> viewer immediately and the run's first `wait_for_user`/intent solicit renders as the bottom
> prompt (`_run_tui_session` wires `user_chat_input_fn → app.solicit`). So §6-B's slash/
> completion test runs right at the first prompt — no need to reach a mid-run question.

---

## 5. The bar — what "correct" means, mapped to the 11 criteria

A correct `--tui` does **all** of:

| # | Criterion | Exercised in | Expected |
|---|-----------|--------------|----------|
| 1 | `--tui` accepted on `imp new` **and** `imp resume`; launches the full-screen viewer on a TTY | §6-A, §6-I | `regime=screen`; no argparse error |
| 2 | Steps render **expanded** (`full=True`) while running, same colors/theme as streaming | §6-C | tool lines like `command=…` visible; matches streaming palette |
| 3 | Auto-follow the tail; PageUp disengages and reveals earlier output; End re-engages | §6-D | PageUp shows the earliest lines; End snaps back to the tail and follows new events |
| 4 | `Ctrl-O` toggles expanded↔lean and re-renders | §6-E | expanded tree collapses to a lean burst summary and back |
| 5 | `ESC` → `signal_pause("esc")` (**pause, not quit**); `Ctrl-C` → interrupt via same coordinator; `EscKeyReader` not started | §6-F, §6-G | clean `paused (esc)` / `paused (ctrl-c)`, `alive=False`, exit 130, resumable |
| 6 | On solicit, a bottom prompt buffer appears with slash handling **and** completion | §6-B | `/help` renders inline; `/sk`+Tab → `/skills`; a plain line submits |
| 7 | `--tui` on a **non-tty** stdout warns and falls back to streaming | §6-K | `--tui needs an interactive terminal; streaming instead.`; **no** alt-screen |
| 8 | On exit, alt-screen restores and a **collapsed (`full=False`)** transcript prints to normal scrollback | §6-H | `regime=scroll`; scrollback has the lean tree (no expanded `command=…` lines) |
| 9 | Session-loop wiring selects the TuiApp path; TTY owned exclusively by the app | §6-A, §6-F | single reader — ESC pauses via the app binding, no double-handling/garbled tty |
| 10 | Covered by the impty harness + subprocess tests | (CI) | `tests/impty/test_tui_app_pty.py` green — note its status, don't re-derive it |
| 11 | `imp resume --tui` replays history expanded & scrollable, then live events fold in | §6-I | resume banner names the same Change; replayed tree scrolls; new events append |

- **Speaks clearly.** Judge the human-visible text/layout with fresh eyes; an unclear, ugly,
  or misleading screen **is a finding** even when behavior is right. `snapshot()` anything visual.
- **No leaked internals / no garbled tty.** No tracebacks, no stray escape sequences in
  scrollback after restore, no `paused: unknown` on an ESC/Ctrl-C pause.

---

## 6. Walk

Run cwd inside `$WT` for every spawn. Keep a tally (criterion → ✅ / ⚠️ / ⏭️) for the report.

### A — launch & alt-screen (crit 1, 9)
`start` `imp new --tui`; `wait()`. `view()` → header should read `regime=screen` with the
cursor parked at the bottom prompt line. `snapshot()`. Judge: header/opening clear, prompt
buffer obviously the place to type, nothing misaligned.

### B — in-app prompt: slash handling + completion (crit 6)
At the opening prompt buffer: `send("/help\r")` → the slash help renders **inside** the app
(above the status bar), not by shelling out. `send("/sk\t")` → completes to `/skills`.
`send("\x15")` (clear), then type a **real, feature-sized** intent so imp does its normal thing
(plans, runs steps, maybe a subagent):
```
send("add an `imp version` subcommand that prints the installed package version, wired into the CLI and argparse help\r")
```
`snapshot()` the prompt with the slash output visible.

### C — expanded render while running (crit 2)
`wait()` until steps stream. `view()` / `snapshot()` → steps render **expanded** (`full=True`):
tool calls show their detail (`command=…`). Confirm the palette matches a streaming run
(spot-check by eye against your memory of `imp new` without `--tui`).

### D — auto-follow + scroll (crit 3)
While running (or after a few steps), `send("\x1b[5~")` (PageUp) → view scrolls up and reveals
the **earliest** output; auto-follow disengaged (new events no longer yank you to the bottom —
verify by `wait()`ing and confirming the viewport stayed put). `send("\x1b[F")` (End) →
re-engages follow, snaps to the tail. `snapshot()` a scrolled-up frame.

### E — Ctrl-O verbosity toggle (crit 4)
`send("\x0f")` → the expanded tree collapses to the lean burst summary (e.g. `N calls · bash`).
`snapshot()`. `send("\x0f")` again → back to expanded.

### F — ESC = pause, not quit (crit 5, 9)
With the agent working, `send("\x1b")` then `wait()` (escape-flush timeout). Expect a clean
**pause**: the app exits, alt-screen restores (`regime=scroll`, `alive=False`), exit 130, and
`view(full=True)` shows `paused (esc)` carrying the change **id** — **not** a quit-without-pause
and **not** `paused: unknown`. Grab the id (`cd "$WT" && imp status`, first column) for §6-I.

### G — Ctrl-C = interrupt (crit 5)
Fresh `imp new --tui`, type an intent, let it work, `send("\x03")`. Same bar as F but
`paused (ctrl-c)`. One Ctrl-C should suffice — no mashing, no hang.

### H — exit → restore + collapsed transcript (crit 8)
After F or G (or a run you let complete), `view(full=True)`: the alt-screen is gone and the
**collapsed** (`full=False`) transcript is in normal scrollback — the lean tree, **no** expanded
`command=…` lines. Confirm scrollback reads like a non-`--tui` run (the alt-screen wipe lost
nothing). `snapshot()` if judging color.

### I — `imp resume --tui` (crit 11)
`start` `imp resume <id> --tui` on the Change you paused in F. Confirm: it continues the **same**
Change (banner names the same `#slug`/id, not `nothing to resume`, not a restart); the replayed
history renders **expanded and scrollable** (PageUp works immediately); then live events fold in
on top. Note header parity with the live run.

### K — non-tty fallback (crit 7)
Pipe stdout so imp sees a non-tty, while stdin stays the tty:
```
start(argv=["bash","-lc","export TERM=xterm-256color; exec imp new --tui 2>&1 | cat"], cwd="<WT>", cols=100, rows=30)
```
`wait()` → expect the warning `imp: --tui needs an interactive terminal; streaming instead.`
and a **normal streaming** run (no alt-screen — `regime` stays `scroll`). Pipes/CI behave
exactly as a non-`--tui` run. `stop()` when satisfied.

### L — theme sweep (palette reuse)
imp ships `dark`, `light`, `ansi` — all shipping UX. Repeat the **visual** screens (C expanded,
E lean) under `IMP_THEME=light` and `IMP_THEME=ansi` (prepend `export IMP_THEME=…;` in the
spawn). `snapshot()` each. The TUI must reuse the streaming palette byte-for-byte: judge that
the theme renders faithfully and `ansi` survives the 16-color downgrade (no lost emphasis,
no glyphs that vanish). Note where a theme reads worse.

---

## 7. Results report

Write to the **main checkout** (survives clone deletion); `mkdir -p` the dir if absent:
```
"$REPO"/docs/bugs/<YYYY-MM-DD>-tui-playbook-results.md
```
Include:
1. **Build under test** — `git -C "$REPO" rev-parse --short HEAD`, whether `--tui` is present,
   provider, themes swept, impty commit.
2. **Criteria matrix** — rows = criteria **1–11** (+ a row each for the light and ansi sweeps);
   cells = ✅ / ⚠️ issue / ⏭️ skipped (one-line reason). Note crit 10 as the CI harness's status.
3. **One section per defect** — title + severity (blocker/major/minor/cosmetic); criterion &
   step; **expected** (cite §5); **what happened** (`view(full=True)` excerpt and/or a
   `snapshot()` PNG path; include `regime`/`alive`/exit, whether one interrupt sufficed);
   **repro** (exact `start` argv, key bytes/timing, config); **notes**.
4. **Clarity findings** — any screen/text that was confusing, ugly, or misleading even where
   behavior was right (attach snapshots).
5. **Skips** — anything skipped and why.

Zero defects is still a report: write the matrix and an explicit "no defects observed" note —
a clean run is a recorded result.

---

## 8. Cleanup — abandon everything

```bash
cd "$WT" && imp status          # enumerate change ids (optional; the clone is discarded wholesale)
cd "$REPO" && rm -rf "$WT"       # the standalone clone holds every branch / nested worktree / journal
git -C "$REPO" status --short    # only the report file should be new
```
If anything in the **main** checkout changed besides the report, flag it — the clone was
supposed to contain all the churn.

---

## 9. Done

Report a one-paragraph wrap-up: build tested (commit + whether the feature is present), which
criteria passed/failed/skipped, the headline findings (especially anything failing the §5 bar),
the report path, and confirmation the clone and all test Changes were abandoned. Then stop.
