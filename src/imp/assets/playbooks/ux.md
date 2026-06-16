---
description: Drive imp's CLI/TUI with fresh eyes and emit a PASS/FAIL UX verdict plus polish suggestions (live, via the impty driver).
---

# imp UX polish playbook — surface-selectable usability pass (impty MCP)

> **You are the test driver.** Drive `imp` *through the impty MCP server*
> (`mcp__impty__start` / `send` / `send_signal` / `view` / `wait` / `snapshot` / `stop`) —
> a real pty with a faithful pyte render — and judge imp's **UX/UI**: usability, readability,
> polish, overall experience. This playbook is **surface-selectable**: run it against the
> **CLI** surface (the streaming command-line UX) or the **TUI** surface (the `--tui`
> full-screen run viewer), or **both**. You are reading the screen with fresh eyes and
> writing down everything confusing, ugly, noisy, or rough — and flagging any functional
> breakage you trip over. Output is an agent-authored **PASS/FAIL verdict** plus an
> open-ended suggestions list. **Do not write any code.**
>
> Read the whole file before you touch a tool. The impty fidelity caveats and the
> throwaway-clone rationale referenced here live once in `tests/playbooks/README.md`.

---

## 0. Mode select — CLI, TUI, or both

Pick the surface for this run **before** §1, and keep it fixed:

- **CLI** — the default streaming command-line UX (`imp new` / `imp resume`, no `--tui`).
  Uses the CLI driving profile (§2) and the CLI must-touch list (§3).
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

### CLI profile (CLI mode)

Spawn imp **directly** — impty makes the spawned process the foreground, controlling-tty
session leader, so no `bash`/`exec` wrapper is needed and keystrokes/signals reach imp:
```
mcp__impty__start(argv=["imp","new"],            cwd="<WT>", cols=100, rows=30)
mcp__impty__start(argv=["imp","resume","<id>"],  cwd="<WT>", cols=100, rows=30)
```
To set variables (theme, or a provider's API key), pass `env=`. **`env` REPLACES the inherited
environment**, so include everything the run needs (e.g. `{**parent_env, "IMP_THEME":"light"}`).
- `send("your text\r")` → type a reply.
- `send_signal(2)` → deterministic SIGINT (a real Ctrl-C) to pause a *running* agent when you
  need a resumable Change for the pause/resume spine step. A keystroke `\x03` mid-run is
  unreliable through impty in cooked mode — see the README fidelity reference.

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
| ESC | `"\x1b"` | **pause** (`signal_pause("esc")`), not quit |
| Ctrl-C | `"\x03"` | interrupt via the same coordinator |
| Tab | `"\t"` | `/command` / skill completion |
| Ctrl-U | `"\x15"` | clear the prompt line |
| Enter | `"\r"` | submit the bottom prompt buffer |

> **ESC escape-flush + keystroke-drop caveats (TUI).** A bare `ESC` is held by
> prompt_toolkit's escape-flush timeout (~0.5s) before it fires — `send("\x1b")` then
> `wait()`. impty can also drop the odd keystroke; if a result looks wrong, re-send once and
> only record it if it persists (note the suspected harness artifact). These are the same
> impty fidelity caveats catalogued in the README.

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
   send("add an `imp version` subcommand that prints the installed package version, wired into the CLI and argparse help\r")
   ```
   Live through plan presentation, step-by-step execution, status/progress. Judge clarity of
   *what imp is doing* and *where you are*. `snapshot()` the plan screen and a mid-execution
   screen.
3. **An in-turn prompt** — when a step asks you a question, read *that* prompt with fresh
   eyes: clear question, obvious default, discoverable affordance? `snapshot()`.
4. **Pause, then `imp resume`** — with the agent working, pause it (CLI: `send_signal(2)`;
   TUI: `send("\x1b")` = ESC) and grab the change id (`cd "$WT" && imp status`, first column).
   Spawn resume in the same mode. Judge: does the resume banner make it obvious which Change
   you're continuing and where it left off? Header parity with the live run?
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
- **Every subcommand** the live `imp --help` lists, **plus each `imp <cmd> -h`** — alignment,
  headers, color, empty-state wording, helpfulness of `-h`. Known commands and what to look at:
  - `imp init` — first-run bootstrap (run in a *fresh* clone with no `.imp/config.yaml`).
  - `imp status` — the Changes table: headers, alignment, id vs slug, empty state.
  - `imp log <id>` — a readable story or a wall of internals?
  - `imp config` / `imp config --explain high` — layer/source arrows legible? precedence clear?
  - `imp abandon <id>` — teardown wording clear? confirmation/affordance?
  - `imp eval -h` — purpose and usage clear from help alone?
  - any command not listed here — cover it too; flag if its UX lags.
- **Empty states** — `imp status` / `imp log` with no Changes yet.
- **`imp init` in a fresh clone** — the real first-run experience.

*TUI mode:*
- **Scroll keys** — PageUp disengages auto-follow and reveals the earliest output; End
  re-engages follow and snaps to the tail (verify auto-follow really disengaged by `wait()`ing
  and confirming the viewport stayed put).
- **Ctrl-O toggle** — expanded tree collapses to a lean burst summary (e.g. `N calls · bash`)
  and back.
- **Slash + Tab completion** — at the bottom prompt buffer `send("/help\r")` renders the slash
  help *inside* the app; `send("/sk\t")` completes to `/skills`; `send("\x15")` clears.
- **ESC-pause** — with the agent working, `send("\x1b")` then `wait()` → clean `paused (esc)`,
  alt-screen restored (`regime=scroll`), `alive=False`, resumable.
- **Ctrl-C-interrupt** — fresh run, type an intent, let it work, `send("\x03")` → clean
  `paused (ctrl-c)`; one Ctrl-C suffices.
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
mode, and themes swept.

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
