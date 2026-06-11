# imp UX playbook — usability & polish pass (impty MCP)

> **You are the test driver.** Drive `imp` *through the impty MCP server*
> (`mcp__impty__start` / `send` / `send_signal` / `view` / `wait` / `snapshot` / `stop`) —
> a real pty with a faithful pyte render — and judge imp's **command-line UX/UI**:
> usability, readability, polish, overall user experience. Walk a full `imp new` /
> `imp resume` cycle on a real feature-sized intent, then sweep every other user-facing
> command. You are *not* hunting crashes — you are reading the screen with fresh eyes and
> writing down everything that's confusing, ugly, noisy, or rough. Output is a list of
> suggestions. **Do not write any code.**
>
> Read the whole file before you touch a tool.

---

## 1. Setup (throwaway clone)

```bash
REPO=$(git rev-parse --show-toplevel)     # main checkout — notes land here, survives clone deletion
WT=/tmp/imp-ux-$(date +%Y%m%d-%H%M%S)
git clone "$REPO" "$WT"
echo "$WT"                                # cwd for every imp run
mkdir -p "$WT/.imp"
cat > "$WT/.imp/config.yaml" <<'YAML'
providers: [claude_code]       # local provider; no API key → no shell wrapper needed (see §2)
theme: dark                    # primary pass; you will also sweep light + ansi (§note)
YAML
cd "$WT" && imp config                    # confirm provider/theme resolved
```

> **Sweep all three themes.** imp ships `dark`, `light`, and `ansi` — `dark`/`light` are
> truecolor; `ansi` is the 16-color theme real users get on limited terminals. All three are
> shipping UX, so review all three. Do the **full** walk (§3) once in `dark`, then repeat the
> visual screens (A–D + each command's output) in `light` and `ansi`, switching with
> `IMP_THEME=light` / `IMP_THEME=ansi` in the spawn env (or by editing `theme:` and
> re-running). impty's renderer captures truecolor *and* 16-color faithfully, so `snapshot()`
> is trustworthy for each. Note where a theme reads worse — low contrast, lost emphasis,
> glyphs that don't survive the 16-color downgrade.

`claude_code` uses the local `claude` session (no API key). If it's unavailable, swap to a
provider you have credentials for — the UX surface is the same.

## 2. Driving primitives

Spawn imp **directly** — impty makes the spawned process the foreground, controlling-tty
session leader, so no `bash`/`exec` wrapper is needed and keystrokes/signals reach imp:
```
mcp__impty__start(argv=["imp","new"],            cwd="<WT>", cols=100, rows=30)
mcp__impty__start(argv=["imp","resume","<id>"],  cwd="<WT>", cols=100, rows=30)
```
To set variables (theme, or a provider's API key), pass `env=` — no shell wrapper needed:
`start(argv=["imp","new"], env={"IMP_THEME":"light"}, cwd="<WT>")`. **`env` REPLACES the
inherited environment**, so include everything the run needs (e.g.
`{**parent_env, "IMP_THEME":"light"}`). Only reach for a `bash -lc` wrapper if you genuinely
need shell features — and then **`exec` is mandatory** (`["bash","-lc","exec imp new"]`),
or bash (not imp) leads the foreground group and Ctrl-C/ESC won't reach imp.

Read:
- `view()` → status header `[alive=… cursor=(x,y)]` + grid. Cursor parked after a prompt = waiting on you.
- `view(full=True)` → whole run so far (scrollback + screen).
- `snapshot()` → PNG; use it to **see** color, glyphs, alignment, spacing when judging polish.

Act:
- `send("your text\r")` → type a reply. `wait()` → settle after output. `stop()` → end a run.
- `send_signal(2)` → deterministic SIGINT (a real Ctrl-C) to pause a *running* agent when you
  need a resumable Change for §3-D (keystroke `\x03` mid-run is unreliable through impty).

> Drive it like a real user: type an intent, watch what renders, react to what you see. You
> don't need a run to finish — a few steps is enough to feel the experience. Abandon at the end.

---

## 3. What to walk

### A — `imp new`, first impressions
Start `imp new`; `wait()` until parked at the first prompt. `view()` + `snapshot()`.
Judge: Is the opening screen clear about what to do next? Banner/header noise vs signal?
Is the prompt obvious and inviting? Any misaligned, low-contrast, or cramped text?

### B — a real, feature-sized intent (the normal experience)
Send something meaty enough that imp does its *normal* thing — plans, runs multiple steps,
maybe dispatches a subagent — not a trivial one-liner. For example:
```
send("add an `imp version` subcommand that prints the installed package version, wired into the CLI and argparse help\r")
```
Then live through the whole flow: plan presentation, step-by-step execution, status/progress,
any tool runs, subagent dispatch. Judge as it unfolds: Is it clear *what imp is doing* and
*where you are* in the run? Does the plan read well? Progress/step lines — signal or noise?
Plumbing steps that should be hidden? Spacing, 100-col wrapping, color legibility, density.
`snapshot()` the plan screen and a mid-execution screen.

### C — an in-turn prompt (imp asks you back)
When a step asks you a question, read **that** prompt with fresh eyes. Judge: is the question
clear, is the default obvious, is the affordance discoverable (arrow-key picker? free text?)?
`snapshot()` it.

### D — pause, then `imp resume`
With the agent working, `send_signal(2)` to pause. Grab the change id:
`cd "$WT" && imp status` (first column). Then spawn `imp resume <id>`.
Judge: does the resume banner make it obvious which Change you're continuing and where it left
off? Header parity with the live run (same slug/title)? Any "starting over" feel, duplicated
headers, or lost context in how it *reads*?

### E — every other command (sweep)
First **enumerate the real surface** so nothing is missed as imp grows:
```bash
cd "$WT" && imp --help                 # lists current subcommands
```
Walk **every** subcommand it lists (not just the ones tabled below), plus `imp <cmd> -h` for
each. Read each output with fresh eyes — alignment, headers, color, empty-state wording,
helpfulness of `-h`. `snapshot()` anything visual. Known commands and what to look at:

| Command | What to judge |
|---|---|
| `imp init` | First-run bootstrap: is detection/output clear? (Run in a *fresh* clone with no `.imp/config.yaml` to see the real first run.) |
| `imp status` | The Changes table: headers, alignment, id vs slug clarity, empty state. |
| `imp log <id>` | The narrative: a readable story, or a wall of internals? |
| `imp config` / `imp config --explain high` | Layer/source arrows: legible? Is precedence understandable? |
| `imp abandon <id>` | Teardown wording: clear about what it removes? Confirmation/affordance? |
| `imp eval -h` | Is the harness's purpose and usage clear from help alone? |
| any command not listed here | Cover it too — judge the same way; flag if its UX lags the others. |
| `imp --help` and each `imp <cmd> -h` | Summaries clear, consistent, well-grouped? Any command undocumented or terse? |

### F — readability sweep (whole run)
`view(full=True)` end to end on the §3-B run. Judge holistically: visual rhythm, consistent
labels/glyphs, color used meaningfully, no leaked internals or debug noise, comfortable density.

---

## 4. Output — suggestions report

Write to the **main checkout** (survives clone deletion):
```
"$REPO"/docs/issues/<YYYY-MM-DD>-imp-ux-notes.md
```

Include:
1. **Build under test** — `git -C "$REPO" rev-parse --short HEAD`, provider, themes swept.
2. **Per-area notes (A–F)** — for each, what you saw and every usability/readability/polish
   suggestion. Attach `snapshot()` PNG paths for anything visual. Call out per-theme
   differences (dark/light/ansi) where they matter.
3. **Top suggestions** — a ranked shortlist (highest-impact first), each with a one-line
   rationale and rough effort.
4. **What worked well** — call out the parts that already read clearly; a clean area is a result.

Frame findings as *suggestions*, not bugs — severity here is "how much it hurts the
experience," not correctness.

## 5. Cleanup
```bash
cd "$REPO" && rm -rf "$WT"
git -C "$REPO" status --short    # only the notes file should be new
```
If anything in the main checkout changed besides the notes file, flag it. Then stop with a
one-paragraph wrap-up: build tested, themes/commands walked, headline suggestions, report path.
