---
description: Drive imp's CLI/TUI with fresh eyes and emit a PASS/FAIL UX verdict plus polish suggestions (live, via the impty driver).
---

# imp UX polish playbook — surface-selectable usability pass (impty MCP)

> **You are the test driver.** Drive `imp` *through the impty MCP server*
> (`mcp__impty__start` / `send` / `send_signal` / `view` / `wait` / `snapshot` / `stop`) —
> a real pty with a faithful pyte render — and judge imp's **UX/UI**: usability, readability,
> polish, overall experience. This playbook is **surface-selectable**: run it against the
> **CLI** surface (the streaming command-line UX, including `imp chat`) or the **TUI**
> surface (the `--tui` full-screen run viewer), or **both**. You are reading the screen with
> fresh eyes and writing down everything confusing, ugly, noisy, or rough — and flagging any
> functional breakage you trip over. Output is an agent-authored **PASS/FAIL verdict** plus an
> open-ended suggestions list. **Do not write any code.**
>
> Read the whole file before you touch a tool. The impty fidelity caveats and the
> throwaway-clone rationale referenced here live once in `tests/playbooks/README.md`.
>
> **Drift guard.** Command inventories and key tables in this file are snapshots; the live
> `imp --help` and the in-app `/help` are authoritative. If they disagree with this playbook,
> follow the live output and **flag the drift in your report** — that finding is itself a
> result.

---

## 0. Mode select — CLI, TUI, or both

Pick the surface for this run **before** §1, and keep it fixed:

- **CLI** — the default streaming command-line UX (`imp new` / `imp chat` / `imp resume`,
  no `--tui`). Uses the CLI driving profile (§2) and the CLI must-touch list (§3).
- **TUI** — the `--tui` full-screen run viewer on the alternate screen. Uses the TUI driving
  profile (§2) and the TUI must-touch list (§3).
- **both** — run the **entire** CLI pass top-to-bottom first and write its report, then run
  the **entire** TUI pass and write its report. Two surfaces, two reports (the `<mode>` token
  in the output path keeps them distinct — §4).

The choice gates the **driving profile** (§2) and the **must-touch list** (§3); the walk
spine (§3) is shared.

---

## 1. Setup (throwaway clone)

```bash
REPO=$(git rev-parse --show-toplevel)     # main checkout — notes land here, survives clone deletion
WT=/tmp/imp-ux-$(date +%Y%m%d-%H%M%S)
git clone "$REPO" "$WT"
echo "$WT"                                # cwd for every imp run
mkdir -p "$WT/.imp"
cat > "$WT/.imp/config.yaml" <<'YAML'
providers: [claude_code]       # local provider; no API key → no shell wrapper needed (CLI). See §2.
theme: dark                    # primary pass; you will also sweep light + ansi (below)
YAML
cd "$WT" && imp config                    # confirm provider/theme resolved
```

> **TUI mode only — disable inherited remote channels for the clean pass.** If your *user*
> config (`~/.config/imp/config.yaml`) enables a `remote.driver` (e.g. telegram), the clone
> inherits it and the channel subprocess writes to **stderr — inherited onto the pty — which
> corrupts the alternate screen**. For the TUI clean pass, add a `remote: {driver: null}`
> block to the config above, and confirm `imp config` shows the channel `(available)`, not
> `(enabled)`. (You re-enable a channel on purpose in the TUI must-touch
> remote-channel-stderr regression, §3. CLI mode does not use the alternate screen, so this
> hazard does not apply there.)

> **Sweep all three themes.** imp ships `dark`, `light`, and `ansi` — `dark`/`light` are
> truecolor; `ansi` is the 16-color theme real users get on limited terminals. All three are
> shipping UX, so review all three. Do the **full** walk (§3) once in `dark`, then repeat the
> visual screens in `light` and `ansi`, switching with `IMP_THEME=light` / `IMP_THEME=ansi`
> in the spawn env (or by editing `theme:` and re-running). impty's renderer captures
> truecolor *and* 16-color faithfully, so `snapshot()` is trustworthy for each. Note where a
> theme reads worse — low contrast, lost emphasis, glyphs that don't survive the downgrade.

`claude_code` uses the local `claude` session (no API key). If it's unavailable, swap to a
provider you have credentials for — the UX surface is the same.

---

## 2. Driving primitives (mode-gated)

Read state (both modes):
- `view()` → status header `[alive=… regime=… cursor=(x,y)]` + grid. `regime=screen` = the
  TUI alt-screen is up; `regime=scroll` = normal scrollback (CLI, or after the TUI restored
  the screen on exit). Cursor parked after a prompt = waiting on you.
- `view(full=True)` → the whole run so far (scrollback + screen).
- `snapshot()` → PNG; use it to **see** color, glyphs, alignment, spacing when judging polish.
- `wait()` → settle after output. `stop()` → end a run.

### The interrupt model (both modes)

imp's uniform interrupt model: **Ctrl-C pauses and stays attached** — the agent stops and you
land at a steer prompt (reply = redirect the agent, which resumes) — and **Ctrl-D leaves**:
at the steer prompt it detaches (rc 130, `alive=False`, Change left paused and resumable);
mid-run it suspends & detaches directly. Interrupt *correctness* (orphans, event
classification, resume targets) is `interrupt-playbook.md`'s job — here you judge the
*experience* of pausing, steering, detaching, and resuming.

### CLI profile (CLI mode)

Spawn imp **directly** — impty makes the spawned process the foreground, controlling-tty
session leader, so no `bash`/`exec` wrapper is needed and keystrokes/signals reach imp:
```
mcp__impty__start(argv=["imp","new"],            cwd="<WT>", cols=100, rows=30)
mcp__impty__start(argv=["imp","chat"],           cwd="<WT>", cols=100, rows=30)
mcp__impty__start(argv=["imp","resume","<id>"],  cwd="<WT>", cols=100, rows=30)
```
To set variables (theme, or a provider's API key), pass `env=`. **`env` REPLACES the inherited
environment**, so include everything the run needs (e.g. `{**parent_env, "IMP_THEME":"light"}`).
- `send("your text\r")` → type a reply.
- `send_signal(2)` → deterministic SIGINT (a real Ctrl-C) to pause a *running* agent. This
  leaves you **attached** at the steer prompt (see the interrupt model above); to get a
  *detached* resumable Change, follow it with `send("\x04")` (Ctrl-D) at that prompt. A
  keystroke `\x03` mid-run is unreliable through impty in cooked mode — see the README
  fidelity reference; `\x04` **at a prompt** is fine (the prompt reads keys directly).

### TUI profile (TUI mode)

`--tui` is a `full_screen=True` prompt_toolkit app on the **alternate screen**. Spawn through
`bash -lc` **with `exec`** so imp (not bash) leads the foreground group and your keystrokes
reach it, and **with `TERM` exported** (without `TERM`, prompt_toolkit degrades and the
full-screen app renders wrong):
```
mcp__impty__start(argv=["bash","-lc","export TERM=xterm-256color; exec imp new --tui"],
                  cwd="<WT>", cols=100, rows=30)
mcp__impty__start(argv=["bash","-lc","export TERM=xterm-256color; exec imp resume <id> --tui"],
                  cwd="<WT>", cols=100, rows=30)
```
`exec` and `TERM` are **both mandatory**. For a theme sweep prepend `export IMP_THEME=light;`
(or `ansi`) before `exec`. `regime` in the `view()` header is your in-TUI / out-of-TUI signal:
`screen` while the viewer is live, `scroll` once it restored the screen on exit.

Key bytes you send in TUI mode:

| Key | `send` data | What it does |
|-----|-------------|--------------|
| PageUp | `"\x1b[5~"` | scroll up, disengage auto-follow |
| PageDown | `"\x1b[6~"` | scroll down |
| End | `"\x1b[F"` (try `"\x1bOF"` / `"\x1b[4~"` if inert) | re-engage auto-follow / jump to tail |
| Ctrl-O | `"\x0f"` | toggle verbosity expanded↔lean |
| Ctrl-C | `"\x03"` | **pause mid-run, stay attached** at the steer prompt (same coordinator as streaming mode) |
| Ctrl-D | `"\x04"` | **leave**: mid-run = suspend & detach; at the steer prompt = detach (alt-screen restored, Change resumable) |
| Tab | `"\t"` | `/command` / skill completion |
| Ctrl-U | `"\x15"` | clear the prompt line |
| Enter | `"\r"` | submit the bottom prompt buffer |

> **ESC is deliberately not a run-viewer key.** The old ESC-pause binding was removed; a bare
> ESC in the viewer should do nothing to the run. ESC still *cancels the inline pickers*
> (no-arg `imp resume` / `imp abandon` — CLI must-touch, §3); there a bare ESC is held by
> prompt_toolkit's escape-flush timeout (~0.5s) before it fires — `send("\x1b")` then
> `wait()`. impty can also drop the odd keystroke; if a result looks wrong, re-send once and
> only record it if it persists (note the suspected harness artifact). These are the same
> impty fidelity caveats catalogued in the README.

> **Mouse wheel (optional).** The viewer enables mouse support; wheel events would arrive as
> SGR mouse reports (`send("\x1b[<64;10;10M")` = wheel-up, `65` = wheel-down). Mouse delivery
> through impty is unproven — treat this as a bonus probe and do **not** grade a
> non-response as a defect.

> Drive it like a real user: type an intent, watch what renders, react to what you see. You
> don't need a run to finish — a few steps is enough to feel the experience. Abandon at the end.

---

## 3. What to walk

### Shared walk spine (both modes)

Run this top-to-bottom once on a real feature-sized intent:

1. **`imp new`** — start a fresh run; `wait()` until parked at the first prompt. Judge first
   impressions: is the opening clear about what to do next? Header noise vs signal? Prompt
   obvious and inviting? `snapshot()`.
2. **A feature-sized intent** — send something meaty enough that imp does its *normal* thing
   (plans, runs multiple steps, maybe dispatches a subagent), e.g.:
   ```
   send("add an `imp doctor` subcommand that checks the project setup (git repo present, providers configured, workflow resolvable) and prints a short report\r")
   ```
   (Pick something that does **not** already exist — check `imp --help` first so the agent
   isn't derailed by "that's already there".) Live through plan presentation, step-by-step
   execution, status/progress. Judge clarity of *what imp is doing* and *where you are*.
   `snapshot()` the plan screen and a mid-execution screen.
3. **An in-turn prompt** — when a step asks you a question, read *that* prompt with fresh
   eyes: clear question, obvious default, discoverable affordance? `snapshot()`.
4. **Pause → steer → detach → `imp resume`** — with the agent working, pause it (CLI:
   `send_signal(2)`; TUI: `send("\x03")`). You stay **attached** at a steer prompt — judge it
   with fresh eyes: is it obvious you can reply to redirect *or* detach? Then detach with
   `send("\x04")` and grab the change id (`cd "$WT" && imp show`, the id column). Spawn
   `imp resume <id>` in the same mode. Judge: does the resume banner make it obvious which
   Change you're continuing and where it left off — and does it land you back where you left
   it? Header parity with the live run?
5. **Sweep every subcommand** — enumerate the real surface so nothing is missed as imp grows
   (`cd "$WT" && imp --help`), then read each subcommand's output with fresh eyes.
6. **Whole-run readability sweep** — `view(full=True)` end to end on the step-2 run. Judge
   holistically: visual rhythm, consistent labels/glyphs, color used meaningfully, no leaked
   internals or debug noise, comfortable density.

### Must-touch list — exercise every item before you judge

These are **prompts to act**, not graded pass/fail rows — skipping them leaves the verdict
uninformed (this is where non-discoverable behaviors survive: as actions to take, not criteria
to score).

*CLI mode:*
- **Bare `imp` and `imp --version`** — the no-command overview is the actual entry point: does
  it orient a brand-new user? Version output sane?
- **Every subcommand** the live `imp --help` lists, **plus each `imp <cmd> -h`** — alignment,
  headers, color, empty-state wording, helpfulness of `-h`. Known commands and what to look at
  (snapshot list — the live `--help` wins; flag drift):
  - `imp init` — first-run bootstrap (run in a *fresh* clone with no `.imp/config.yaml`);
    it's interactive — judge its prompts and the theme detection.
  - `imp new -h` — the flag surface (`--backlog`, `--depends-on`, `--workflow`,
    `--autonomous`, `--non-interactive`/`--message`): can a new user tell which they need?
  - `imp chat` — the conversation surface (own item below).
  - `imp show` — the overview: zones (in-flight / Backlog / recently merged) legible? ids vs
    titles? Then `imp show <id>` (detail card) and one artifact view (e.g.
    `imp show <id> status`), plus `-a`.
  - `imp log <id>` — a readable story or a wall of internals? Also `imp log <id> --summary`
    (the per-step timeline) and `--full`.
  - `imp search <query>` — both a hit and a miss: is the no-match wording helpful?
  - `imp config` / `imp config --explain high` — layer/source arrows legible? precedence clear?
  - `imp abandon <id>` — teardown wording clear? confirmation/affordance?
  - `imp edit <id> --title "…"` — is the confirmation clear about what changed?
  - `imp remote-control -h` — purpose clear from help alone? (The full chat UX has its own
    playbook — don't drive the daemon here.)
  - any command not listed here — cover it too; flag if its UX lags **and flag this list as
    stale in your report**.
- **The pager** — `imp log` / `imp show` page through `less` on a tty: a long narrative holds
  the screen (send `"q"` to quit); `--no-pager` prints straight through (use it or
  `view(full=True)` when *you* need to read). Judge that paging feels intentional, not broken.
- **Slash commands at the interactive prompt** — at any imp prompt: `/help` (readable, and
  does it match reality?), `/skills`, `/list`, `/search <q>`; Tab completion (`send("/sk\t")`
  → `/skills`); Up-arrow recalls your previous input; Alt+Enter (`send("\x1b\r")`) inserts a
  newline instead of submitting; Ctrl-C at the prompt clears the buffer (and hints at Ctrl-D
  once); and paste a bare path like `/home/user/foo.py` — it must submit as ordinary input,
  **not** error as an unknown command.
- **`imp chat`** — the lightweight conversation surface: open it, exchange a couple of turns
  (ask something about the repo), then `/save` — is the saved-to-Backlog messaging clear? Try
  `/clear` (parks the chat, starts fresh). Also compare Ctrl-D dispositions: after an
  exchange (saves) vs in a brand-new chat with no exchange (cancels). Does chat read as a
  distinct, lighter thing than `imp new`?
- **Inline pickers** — no-arg `imp resume` and no-arg `imp abandon` open an arrow-key picker:
  `send("\x1b[B")` / `send("\x1b[A")` to move, Enter selects, ESC cancels (escape-flush
  caveat, §2). Judge the candidate rows: enough context to pick confidently?
- **Backlog flow** — `imp new --backlog "some idea"` files an idea and exits: does the
  message tell you how to start it later? Confirm it appears in `imp show`'s Backlog zone.
- **Empty states** — `imp show` / `imp log` before any Change exists.
- **`NO_COLOR=1`** — rerun `imp log <id>` with `NO_COLOR=1` in the env: fully legible, no
  meaning carried by color alone?
- **Log exports** — `imp log <id> --md -` (read it: honest, well-structured Markdown?) and
  `imp log <id> --html <file>` (spot-check the file: self-contained, opens as one page?).
- **Narrow terminal (bonus)** — repeat one or two visual screens at `cols=80`: wrapping,
  clipped tables, broken alignment.
- **`imp init` in a fresh clone** — the real first-run experience.

*TUI mode:*
- **Scroll keys** — PageUp disengages auto-follow and reveals the earliest output; End
  re-engages follow and snaps to the tail (verify auto-follow really disengaged by `wait()`ing
  and confirming the viewport stayed put).
- **Ctrl-O toggle** — expanded tree collapses to a lean burst summary (e.g. `N calls · bash`)
  and back.
- **Slash + Tab completion** — at the bottom prompt buffer `send("/help\r")` renders the slash
  help *inside* the app; `send("/sk\t")` completes to `/skills`; `send("\x15")` clears.
- **Ctrl-C pause → steer (attached)** — with the agent working, `send("\x03")` then `wait()`
  → the run pauses and you're at a steer prompt, still attached. Judge the paused state's
  wording: is it obvious you can reply to redirect, or detach? Send a short steer reply and
  confirm the agent visibly resumes with it.
- **Ctrl-D detach** — pause again (or straight from mid-run) and `send("\x04")` → clean exit:
  alt-screen restored (`regime=scroll`), `alive=False`, Change left paused and resumable.
  One keystroke suffices.
- **Stray ESC is inert** — while the viewer is live, `send("\x1b")` + `wait()`: the run must
  keep going — ESC is not a pause/quit key anymore; a stray ESC must not nuke the run.
- **Exit → collapsed transcript** — after a pause or completion, `view(full=True)`: the
  alt-screen is gone and a **collapsed** transcript (lean tree, no expanded `command=…` lines)
  is in normal scrollback — reads like a non-`--tui` run, lost nothing.
- **Non-tty fallback** — pipe stdout so imp sees a non-tty while stdin stays the tty:
  `start(argv=["bash","-lc","export TERM=xterm-256color; exec imp new --tui 2>&1 | cat"], cwd="<WT>", cols=100, rows=30)`.
  Expect `imp: --tui needs an interactive terminal; streaming instead.` and a normal streaming
  run (no alt-screen — `regime` stays `scroll`).
- **Remote-channel-stderr regression** — re-enable a channel
  (`printf 'providers: [claude_code]\ntheme: dark\nremote:\n  driver: telegram\n' > "$WT/.imp/config.yaml"`)
  and spawn `imp new --tui` (no valid token needed — a failing poll is the stress). Expect a
  clean viewer: **no** `imp-telegram: …` / channel stderr on the alt-screen, no garbled body,
  status bar stays the last line. Restore `remote: {driver: null}` afterward.
- **`resume <id> --tui` replay** — on a paused Change: replays history expanded & scrollable
  (PageUp works immediately), then live events fold in on top.
- **Mouse wheel (bonus)** — try the SGR wheel reports from §2; scrolling is a nice
  confirmation, but a non-response is a harness caveat, not a defect.

---

## 4. Output — PASS/FAIL verdict + suggestions

Write **one** report to the main checkout (survives clone deletion). The `<mode>` token is the
surface you ran (`cli` or `tui`; for **both**, write one file per surface):
```
"$REPO"/docs/issues/<YYYY-MM-DD>-imp-ux-<mode>-notes.md
```

Open with the **verdict**, then functional findings, then suggestions — in this order:

1. **PASS / FAIL verdict** — your own holistic judgment against these seeded guideline
   categories (state PASS or FAIL on each, with a one-line justification):
   - **Clarity** — did everything read clearly; is it obvious what imp is doing and where you are?
   - **Unexpected functional breakage** — did anything break, hang, crash, or misbehave while you drove it?
   - **Leaked internals / garbled tty** — tracebacks, stray escape sequences, debug noise, or
     (TUI) a corrupted alternate screen.
   The overall verdict is **FAIL** if any category fails; otherwise **PASS**. (Exclusive TTY
   ownership is not a gradable row — flag it only via its garbled-tty symptom. The CI harness's
   status is noted, not tested here.)
2. **Functional findings** — anything that broke or misbehaved, each with repro (exact `start`
   argv, keystrokes, config) and `view(full=True)` / `snapshot()` evidence.
3. **Polish suggestions** — the open-ended list: per-area usability/readability/polish notes
   with `snapshot()` PNG paths for visuals and per-theme (dark/light/ansi) differences; a
   ranked top-suggestions shortlist (highest-impact first, one-line rationale + rough effort);
   and a "what worked well" call-out (a clean area is a result).

Also record the **build under test** (`git -C "$REPO" rev-parse --short HEAD`), provider, the
mode, themes swept, and any **playbook drift** you hit (commands or keys where the live
surface disagreed with this file).

Frame polish items as *suggestions* (severity = how much it hurts the experience); frame
functional findings as *defects* (severity = correctness).

---

## 5. Cleanup
```bash
cd "$REPO" && rm -rf "$WT"
git -C "$REPO" status --short    # only the notes file(s) should be new
```
If anything in the main checkout changed besides the notes file(s), flag it. Then stop with a
one-paragraph wrap-up: build tested, mode + themes/commands walked, the PASS/FAIL verdict,
headline suggestions, report path.
