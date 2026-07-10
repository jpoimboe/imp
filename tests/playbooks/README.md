# Playbooks — agent-driven acceptance tests

A **playbook** is a prose acceptance test written for an *agent* (Claude) to execute by driving
the real `imp` through the **impty** MCP pty harness. Unlike the Python suite next door
(`tests/`, `tests/impty/`), a playbook is not collected by pytest — it is a self-contained
instruction set the agent follows to drive imp and judge whether it behaves *and speaks*
correctly.

## Why playbooks (and not pytest)

Playbooks shine for two kinds of test that are hard to express as deterministic Python:

1. **Judgment-heavy tests.** "Is this screen clear? is the wording good? does this read like a
   continuation?" needs a reader with taste, not an `assert`. A playbook lets an agent judge the
   UX with fresh eyes.
2. **End-to-end tests that are hard to make deterministic.** Driving the real `imp` against a
   real provider through a real pty exercises paths a unit test can't, but provider
   non-determinism makes a hard-coded assertion brittle. A playbook trades a fixed assertion for
   an agent's on-the-spot correctness check.

## The two kinds in this suite

- **Surface polish pass** (`imp-ux-playbook.md`) — open-ended, judgment-led. Walk the CLI
  surface with fresh eyes and emit an agent-authored **PASS/FAIL verdict** plus a
  suggestions list → `docs/issues/`.
- **Correctness matrix** (`interrupt-playbook.md`) — exhaustive and binary. Fire every
  (provider × state × trigger) combination, grep for orphans and event-log classification,
  pass/fail each cell → `docs/bugs/`.

## Conventions

- **A playbook is durable and re-runnable**, named `<name>-playbook.md`, with no date prefix
  (unlike dated specs/plans/runbooks). Most playbooks pair 1:1
  with a feature spec in `imp/specs/` and are named after it. **But a playbook may instead be a
  *surface* or *cross-cutting* test** — `imp-ux-playbook.md` is a CLI-surface pass
  and not tied to a single feature. Name such a playbook after the surface or concern it covers,
  not one feature.
- **The agent drives `imp` through the impty MCP server** (`mcp__impty__start` / `send` /
  `send_signal` / `view` / `wait` / `snapshot` / `stop`) — a real pty with a faithful pyte
  render. Full scroll-back is `view(full=True)`, not a separate tool. See the shared impty
  fidelity reference below.
- **Run in a throwaway standalone `git clone`** — see the throwaway-clone reference below.
- **Reports land in the main checkout** so they survive clone deletion: correctness matrices →
  `docs/bugs/<YYYY-MM-DD>-<name>-playbook-results.md`; polish passes →
  `docs/issues/<YYYY-MM-DD>-imp-ux-cli-notes.md`. **A clean run is still a report** — write
  the matrix/verdict and an explicit "no defects observed" note. A passing run that produces no
  artifact is the most likely failure of this convention.

## Shared impty fidelity reference

The playbooks restate the driving primitives they need, but the canonical, rarely-changing
fidelity caveats live here once:

- **Keystrokes vs signals.** impty claims its pty slave as imp's controlling terminal, so
  keystrokes you `send` reach imp's tty. Prefer `send` for Ctrl-C/ESC so you exercise imp's real
  tty paths; `send_signal(sig=…)` (SIGINT=2, SIGTERM=15) is the out-of-band path that signals
  imp's process group directly.
- **Cooked-mode mid-run caveat.** `send("\x03")` only becomes a SIGINT when the tty line
  discipline has ISIG on (cooked/cbreak); at a prompt_toolkit prompt it is a raw byte. impty
  also degrades prompt_toolkit's CPR handshake, so in **cooked-mode** runs mid-run keystroke
  Ctrl-C/ESC delivery is unreliable — use `send_signal(2)` there. ESC has no signal equivalent,
  so the keystroke is always the faithful ESC test.
- **`exec` in shell-wrapped spawns.** If you spawn through `bash -lc`, `exec` is mandatory —
  without it bash (not imp) leads the foreground group and Ctrl-C/ESC never reach imp.

## Shared fake-Telegram harness reference

For the **chat** surface, `tests/playbooks/fake_telegram.py` plays the role impty plays for the
pty: a stdlib `http.server` fake Bot API that is the test's window into the chat. It implements
exactly the two methods the adapter calls — `POST /bot<token>/getUpdates` (FIFO drain honouring the
adapter's offset, monotonic `update_id`s) and `POST /bot<token>/sendMessage` (records outbound,
monotonic `message_id` so reply-to threading is faithful) — plus a test side-channel: `POST
/_inject` (push inbound text, optional `reply_to_message_id`; malformed body → 400) and `GET
/_outbox` (the messages the bot sent). Unlike impty it needs no MCP server: JSON-over-HTTP is
already agent-drivable from bash with `curl`. The deterministic pytest drives it through its Python
API (`start`/`url`/`inject`/`inject_reply`/`outbox`/`stop`); the playbook launches it with `python3
-m tests.playbooks.fake_telegram` and drives it with `curl`. Point a real daemon at it via
`IMP_TELEGRAM_API_BASE`.

## Throwaway-clone reference

Run in a **standalone `git clone`** (not a `git worktree add`) and abandon all churn at the end.
A clone supplies cwd plus a discardable imp project whose control root is *itself* — so a
local-layer config override (`.imp/config.yaml`, e.g. to force a provider) both **wins** and
**stays isolated**. A linked worktree can't guarantee that: its control root resolves to the
main checkout (`git rev-parse --git-common-dir` → parent), so a local override would pollute the
shared tree. Test against the installed `imp` like every other test — don't hard-code internal
symbols or branch paths into a playbook.

## Index

*(Append every new playbook here — this list is hand-maintained.)*

- `before-after-playbook.md` — before/after visual showcase for an imp change: render the
  user-visible CLI surfaces it touched, before vs after, into one self-contained HTML page.
  A communication artifact, not a pass/fail test.
- `dogfood-playbook.md` — run-to-completion outcome/e2e acceptance test: drive a full `imp new` run
  to completion + merge, judge the produced work with fresh eyes, run `check_dogfood_invariants.py`,
  and write a critical PASS/FAIL results report → `docs/bugs/`.
- `remote-control-playbook.md` — drive a full Change from the chat (fake Telegram harness + real
  `imp remote-control start` via `IMP_TELEGRAM_API_BASE`) to completion + merge, exercise `/help` /
  `/list` / `/new` / `/abandon` / the unknown-command notice / the unroutable-message hint, judge
  the multi-Change chat UX, and write a PASS/FAIL + UX verdict → `docs/bugs/`. Validates
  `imp/specs/2026-06-14-remote-control-chat-commands.md`.
- `imp-ux-playbook.md` — surface polish pass: walk the CLI surface with fresh eyes and emit an
  agent-authored PASS/FAIL verdict plus a suggestions list → `docs/issues/`.
- `interrupt-playbook.md` — correctness matrix: fire every (provider × state × trigger)
  combination, grep for orphans and event-log classification, pass/fail each cell → `docs/bugs/`.
