---
description: Drive imp's CLI with fresh eyes and emit a PASS/FAIL UX verdict plus polish suggestions (live, via the impty driver).
---

# imp UX polish playbook — CLI usability pass (impty MCP)

> **You are the test driver.** Drive `imp` *through the impty MCP server*
> (`mcp__impty__start` / `send` / `send_signal` / `view` / `wait` / `snapshot` / `stop`) —
> a real pty with a faithful pyte render — and judge imp's **UX/UI**: usability, readability,
> polish, overall experience. This playbook drives the **CLI** surface — the streaming
> command-line UX, including `imp chat`. You are reading the screen with
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

## 1. Setup (throwaway clone)

```bash
REPO=$(git rev-parse --show-toplevel)        # main checkout — cloned from; stays clean
REPORTS=${IMP_PLAYBOOK_REPORTS:-${IMP_HOME:-$HOME/.imp}/playbook-reports/$(basename "$REPO")}
mkdir -p "$REPORTS"                          # reports land here, outside every checkout
SHOTS="$REPORTS/$(date +%F)-imp-ux-shots"; mkdir -p "$SHOTS"; echo "$SHOTS"   # snapshot dir — note the absolute path
WT=${TMPDIR:-/tmp}/imp-ux-$(date +%Y%m%d-%H%M%S)
git clone "$REPO" "$WT"
echo "$WT"                                # cwd for every imp run
cd "$WT" && imp config set --local theme dark  # claims this clone's state dir; the heredoc below overwrites the file
STATE="${IMP_HOME:-$HOME/.imp}/projects/$(basename "$WT")"
test -f "$STATE/repo"                     # the claim marker — missing means imp claimed a different slug
cat > "$STATE/config.yaml" <<'YAML'
providers: [claude]            # local provider; no API key → no shell wrapper needed (CLI). See §2.
theme: dark                    # primary pass; you will also sweep light + ansi (below)
YAML
cd "$WT" && imp config                    # confirm provider/theme resolved; label must read `[$STATE/config.yaml]`
                                          # (shown as `[~/.imp/projects/<clone-name>/config.yaml]` when IMP_HOME is under $HOME, else absolute)
```

> **Sweep all three themes.** imp ships `dark`, `light`, and `ansi` — `dark`/`light` are
> truecolor; `ansi` is the 16-color theme real users get on limited terminals. All three are
> shipping UX, so review all three. Do the **full** walk (§3) once in `dark`, then repeat the
> visual screens in `light` and `ansi`, switching with `IMP_THEME=light` / `IMP_THEME=ansi`
> in the spawn env (or by editing `theme:` and re-running). impty's renderer captures
> truecolor *and* 16-color faithfully, so `snapshot()` is trustworthy for each. Note where a
> theme reads worse — low contrast, lost emphasis, glyphs that don't survive the downgrade.

`claude` uses the local `claude` session (no API key). If it's unavailable, swap to a
provider you have credentials for — the UX surface is the same.

---

## 2. Driving primitives

Read state:
- `view()` → status header `[alive=… regime=… cursor=(x,y)]` + grid; for the streaming CLI
  `regime=scroll` (normal scrollback). Cursor parked after a prompt = waiting on you.
- `view(full=True)` → the whole run so far (scrollback + screen).
- `snapshot(path="<SHOTS>/<theme>-<screen>.png")` → PNG; use it to **see** color, glyphs,
  alignment, spacing when judging polish. Save **every** snapshot this way, with `<SHOTS>` the
  literal absolute path echoed at setup — the tool expands no `$SHOTS` and creates no parent dirs.
- `wait()` → settle after output. `stop()` → end a run.

### The interrupt model

imp's uniform interrupt model: **Ctrl-C pauses and stays attached** — the agent stops and you
land at a steer prompt (reply = redirect the agent, which resumes) — and **Ctrl-D leaves**:
at the steer prompt it detaches (rc 130, `alive=False`, Change left paused and resumable);
mid-run it suspends & detaches directly. Interrupt *correctness* (orphans, event
classification, resume targets) is `interrupt-playbook.md`'s job — here you judge the
*experience* of pausing, steering, detaching, and resuming.

### CLI profile

Spawn imp **directly** — impty makes the spawned process the foreground, controlling-tty
session leader, so no `bash`/`exec` wrapper is needed and keystrokes/signals reach imp:
```
mcp__impty__start(argv=["imp","new"],            cwd="<WT>", cols=100, rows=30)
mcp__impty__start(argv=["imp","chat"],           cwd="<WT>", cols=100, rows=30)
mcp__impty__start(argv=["imp","resume","<id>"],  cwd="<WT>", cols=100, rows=30)
```
To set variables (theme, or a provider's API key), pass `env=`. **`env` entries are ADDED to the
inherited environment**, overriding same-named ones — so pass just what you need (e.g.
`{"IMP_THEME":"light"}`).
- `send("your text\r")` → type a reply.
- `send_signal(2)` → deterministic SIGINT (a real Ctrl-C) to pause a *running* agent. This
  leaves you **attached** at the steer prompt (see the interrupt model above); to get a
  *detached* resumable Change, follow it with `send("\x04")` (Ctrl-D) at that prompt. A
  keystroke `\x03` mid-run is unreliable through impty in cooked mode — see the README
  fidelity reference; `\x04` **at a prompt** is fine (the prompt reads keys directly).

> **ESC cancels the inline pickers.** At a no-arg `imp resume` / `imp abandon` picker (CLI
> must-touch, §3), a bare ESC is held by prompt_toolkit's escape-flush timeout (~0.5s) before
> it fires — `send("\x1b")` then `wait()`. impty can also drop the odd keystroke; if a result
> looks wrong, re-send once and only record it if it persists (a suspected harness artifact).
> These are the same impty fidelity caveats catalogued in the README.

> Drive it like a real user: type an intent, watch what renders, react to what you see. You
> don't need a run to finish — a few steps is enough to feel the experience. Abandon at the end.

---

## 3. What to walk

### Walk spine

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
   isn't derailed by "that's already there".) Live through the planning phase (headed
   `planning phase · plan N/M`), step-by-step execution, status/progress. Judge clarity of
   *what imp is doing* and *where you are*. `snapshot()` the planning-phase screen and a
   mid-execution screen.
3. **An in-turn prompt** — when a step asks you a question, read *that* prompt with fresh
   eyes: clear question, obvious default, discoverable affordance? `snapshot()`.
4. **Pause → steer → detach → `imp resume`** — with the agent working, pause it
   (`send_signal(2)`). You stay **attached** at a steer prompt — judge it
   with fresh eyes: is it obvious you can reply to redirect *or* detach? Then detach with
   `send("\x04")` and grab the change id (`cd "$WT" && imp list`, the id column). Spawn
   `imp resume <id>`. Judge: does the resume banner make it obvious which
   Change you're continuing and where it left off — and does it land you back where you left
   it? Below the banner, do the agent's last message and what followed it replay, with
   earlier turns absent? Header parity with the live run? Then run `imp log <id>`. Judge: does a
   `paused · resumed … later` line sit where the run stopped, with a duration matching the
   time you were away?
5. **Sweep every subcommand** — enumerate the real surface so nothing is missed as imp grows
   (`cd "$WT" && imp --help`), then read each subcommand's output with fresh eyes.
6. **Whole-run readability sweep** — `view(full=True)` end to end on the step-2 run. Judge
   holistically: visual rhythm, consistent labels/glyphs, color used meaningfully, no leaked
   internals or debug noise, comfortable density.

### Must-touch list — exercise every item before you judge

These are **prompts to act**, not graded pass/fail rows — skipping them leaves the verdict
uninformed (this is where non-discoverable behaviors survive: as actions to take, not criteria
to score).

- **Bare `imp` and `imp --version`** — the no-command overview is the actual entry point: does
  it orient a brand-new user? Version output sane?
- **Every subcommand** the live `imp --help` lists, **plus each `imp <cmd> -h`** — alignment,
  headers, color, empty-state wording, helpfulness of `-h`. Known commands and what to look at
  (snapshot list — the live `--help` wins; flag drift):
  - `imp init` — first-run bootstrap. A repo with `imp/config.yaml` already committed (such as a
    clone of an already-init'd imp repo) short-circuits to "Project already initialized"; a
    machine whose `~/.imp/config.yaml` already lists providers skips only the picker. To see the
    real interactive path, run in a **fresh non-imp git repo** with `IMP_HOME` pointed at an
    empty dir. The **provider multi-select** comes first, then the **confirmation screen**
    (detected settings, `enter` to accept) — both interactive; theme is **auto-detected** with
    no prompt: `ansi` unless `COLORTERM` is `truecolor`/`24bit`, else `dark`/`light` from the
    terminal background (default `dark`). A `dark`/`light` pick prints one dim line,
    `Detected a dark terminal — colors tuned to match.` (or `light`); the `ansi` path prints
    nothing. impty advertises truecolor, so expect `dark` and that line; to see the silent
    `ansi` path, spawn with `env={"COLORTERM":""}`.
  - `imp new -h` — the flag surface (`--title`, `--backlog`, `--background`, `--depends-on`,
    `--workflow`, `--var`, `--autonomous`, `--message`, `--base`,
    `--provider`): can a new user tell which they need?
  - `imp chat` — the conversation surface (own item below).
  - `imp resume -h` — the flag surface; the resume itself is driven in the walk spine (step 4)
    and the picker under Inline pickers.
  - `imp list` — the overview: zones (in-flight / backlog / completed; `history` under `-a`)
    legible? ids vs titles? Then `imp show <id>` (detail card, whose `files:` block lists the
    quest's saved files) and its saved-file views: `imp show <id> spec`, `imp show <id> verify`
    (the last failing test-gate run) and `summary` each print their folder's one file, `plans`
    prints the plan when there is only one and lists them when there are several, and a listed
    path prints that file. A name matching nothing (e.g. `status`) errors and points back at
    `imp show <id>`. A plan's task list (`✓ plan N/M · K tasks · imp show <id> plans/…`)
    prints only once the plan is finalized (after plan review passes, or on the trivial-quest
    fast path) — a run stopped during plan review never shows it; not a defect. Also
    `imp list -a`.
  - `imp log <id>` — a readable story or a wall of internals? Also `imp log <id> --summary`
    (the per-step timeline) and `--full`.
  - `imp events <id>` — the event-log index/filter surface (`--type`, `--wire`, `--range`):
    expert-oriented, but read its `-h` and a bare invocation.
  - `imp events search <query>` — event-content search **across** quests; it takes no quest
    id (narrow it with the zone/status flags its `-h` lists). Read its `-h` and run a hit and
    a miss.
  - `imp search <query>` — both a hit and a miss: is the no-match wording helpful?
  - `imp config` / `imp config --explain high` — layer/source labels legible? precedence clear?
  - `imp config set` / `imp config unset` (and each `-h`) — **always pass `--local`**: with no
    scope flag, machine-wide keys like `theme` land in your real `~/.imp/config.yaml`. Run
    `imp config set --local theme light`, then `imp config` (theme now `light`, still from the
    local layer), then `imp config unset --local theme` (comments the line out) and `imp config`
    again (theme falls back to the global layer or default), then
    `imp config set --local theme dark` to restore the pass's theme. Is the confirmation clear
    about what changed and where? Is the source shift after `unset` obvious in `imp config`?
  - `imp abandon <id>` — teardown wording clear? confirmation/affordance?
  - `imp edit <id> --title "…"` — is the confirmation clear about what changed?
  - `imp remote-control -h` — purpose clear from help alone? (The full chat UX has its own
    playbook — don't drive the daemon here.)
  - `imp help` and `imp help <command>` — do they match `imp --help` and `imp <command> -h`?
  - any command not listed here — cover it too; flag if its UX lags **and flag this list as
    stale in your report**.
- **The pager** — `imp log` / `imp list` page through `less` on a tty: a long narrative holds
  the screen (send `"q"` to quit); `--no-pager` prints straight through (use it or
  `view(full=True)` when *you* need to read). Judge that paging feels intentional, not broken.
- **Slash commands at the interactive prompt** — at any imp prompt, including the opening
  `imp new` / `imp chat` intent prompt and the post-`/clear` prompt. The live `/help` is
  authoritative (read it and check it matches reality). The real client-side set is smaller
  than a "full nine": `/help`, `/skills`, `/model <m>`, `/sandbox [on|off]`,
  `/provider [name]`, `/abandon`, and `/<skill>` at **every** prompt. `/compact` applies only
  once a conversation is running. `/pause`, `/archive`, and `/clear` also need a running
  conversation; `/archive` and `/clear` only while the quest is not yet built (`imp chat` or
  any pre-materialized conversation). On a built quest they print a redirect naming
  `/pause`, and `/pause` pauses it like Ctrl-D (`· paused · /pause`). The retired `save` and
  `shelve` leave names are unknown commands now (`Unknown command: …`). At a pre-session
  prompt (opening intent, post-`/clear`, inert-resume reply) `/help` still lists
  `/compact`, `/pause`, `/archive`, and `/clear`, but each prints a redirect and re-prompts
  when typed. **There is no `/list`, `/search`, or `/resume`** client-side. Each is handled
  without spending a model turn; Tab completion covers them (`send("/sk\t")` → `/skills`), an
  unambiguous prefix fills and an ambiguous one lists candidates, then further Tabs cycle them;
  Up-arrow recalls previous input; Alt+Enter (`send("\x1b\r")`) inserts a newline;
  Ctrl-C clears a typed line; only on an already-empty line does it print
  `press Ctrl-D to pause & exit` (once per prompt, and never at a prompt whose placeholder
  already names Ctrl-D). **Note** (see `slash.py`): an unmatched
  command-shaped `/<name>` (e.g. `/list`) prints `Unknown command: /<name> — /help for the list`
  and re-prompts; only a path-shaped token (holding `/` or `.`, e.g. `/home/user/foo.py`) is
  submitted to the model verbatim. Test both: a typo errors client-side, and a pasted path submits
  as ordinary input.
- **`imp chat`** — the lightweight conversation surface: open it, exchange a couple of turns
  (ask something about the repo), then `/pause`. The block should end `· paused · /pause` and the
  chat should stay in `imp list`. In fresh chats, after an exchange, try `/archive` and `/clear`
  (`/clear` also starts a fresh one): each should end a bare `· archived`, and the chat leaves
  `imp list` but shows under `imp list --archived`. Also compare Ctrl-D dispositions: after an
  exchange in an untitled chat (`archived · Ctrl-D`) vs in a brand-new chat with no exchange
  (`discarded`).
  Every leave here is rc 130. On a built quest, `/pause` at a prompt should print
  `<id> · <name> · paused · /pause` and leave it `paused`, and `imp resume` should land where
  Ctrl-D would. Does chat read as a distinct, lighter thing than `imp new`?
- **Inline pickers** — no-arg `imp resume` and no-arg `imp abandon` open an arrow-key picker:
  `send("\x1b[B")` / `send("\x1b[A")` to move, Enter selects. The picker **advertises `q` to
  cancel** (a bare `q` keystroke); ESC also cancels (escape-flush caveat, §2). A cancel erases
  the picker, prints `imp: resume cancelled` / `imp: abandon cancelled`, and exits 130. Judge the
  candidate rows: enough context to pick confidently?
- **backlog flow** — `imp new --backlog "some idea"` files an idea and exits: does the
  message tell you how to start it later? Confirm it appears in `imp list`'s backlog zone.
- **Empty states** — `imp list` / `imp log` before any Change exists.
- **`NO_COLOR=1`** — rerun `imp log <id>` with `NO_COLOR=1` in the env: fully legible, no
  meaning carried by color alone?
- **Log exports** — `imp log <id> --md -` (read it: honest, well-structured Markdown?) and
  `imp log <id> --html <file>` (spot-check the file: self-contained, opens as one page?).
- **Narrow terminal (bonus)** — repeat one or two visual screens at `cols=80`: wrapping,
  clipped tables, broken alignment.
- **`imp init` in a fresh clone** — the real first-run experience.

---

## 4. Output — PASS/FAIL verdict + suggestions

Write **one** report to `$REPORTS` (outside every checkout; survives clone deletion):
```
"$REPORTS"/<YYYY-MM-DD>-imp-ux-cli-notes.md
```

Open with the **verdict**, then functional findings, then suggestions — in this order:

1. **PASS / FAIL verdict** — your own holistic judgment against these seeded guideline
   categories (state PASS or FAIL on each, with a one-line justification):
   - **Clarity** — did everything read clearly; is it obvious what imp is doing and where you are?
   - **Unexpected functional breakage** — did anything break, hang, crash, or misbehave while you drove it?
   - **Leaked internals / garbled tty** — tracebacks, stray escape sequences, or debug noise.
   The overall verdict is **FAIL** if any category fails; otherwise **PASS**. (The CI harness's
   status is noted, not tested here.)
2. **Functional findings** — anything that broke or misbehaved, each with repro (exact `start`
   argv, keystrokes, config) and `view(full=True)` / `snapshot()` evidence.
3. **Polish suggestions** — the open-ended list: per-area usability/readability/polish notes
   with relative image links for visuals (e.g.
   `![planning-phase screen, dark](<YYYY-MM-DD>-imp-ux-shots/dark-plan.png)`) and per-theme
   (dark/light/ansi) differences; a
   ranked top-suggestions shortlist (highest-impact first, one-line rationale + rough effort);
   and a "what worked well" call-out (a clean area is a result).

Also record the **build under test** (`git -C "$REPO" rev-parse --short HEAD`), provider,
themes swept, and any **playbook drift** you hit (commands or keys where the live
surface disagreed with this file).

Frame polish items as *suggestions* (severity = how much it hurts the experience); frame
functional findings as *defects* (severity = correctness).

---

## 5. Cleanup
```bash
STATE="${IMP_HOME:-$HOME/.imp}/projects/$(basename "$WT")"
[ -d "$WT" ] && [ "$(cat "$STATE/repo" 2>/dev/null)" = "$(cd "$WT" && pwd -P)" ] && rm -rf "$STATE"   # this clone's state only
cd "$REPO" && rm -rf "$WT"
git -C "$REPO" status --short   # must print nothing — reports live in $REPORTS
```
If it prints anything, flag it. Then stop with a one-paragraph wrap-up: build tested,
themes/commands walked, the PASS/FAIL verdict, headline suggestions, the report's absolute path.
