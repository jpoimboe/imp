# Playbooks — agent-driven acceptance tests

A **playbook** is a prose acceptance test for a feature, written for an *agent* (Claude)
to execute. Unlike the Python suite next door (`tests/`, `tests/impty/`), a playbook is
not collected by pytest: it is a self-contained instruction set that an agent follows to
drive the real `imp` and judge whether it behaves — and *speaks* — correctly.

## Conventions

- **One file per feature**, named `<feature>-playbook.md`. No date prefix: a playbook is a
  durable, re-runnable contract that tracks its feature across builds (unlike the dated
  specs/plans/runbooks under `docs/superpowers/`). It pairs 1:1 with a spec in
  `imp/specs/` — name it after the same feature so the pairing is obvious. The `<feature>`
  token is identical across the spec, the playbook, and the results file.
- **The agent drives `imp` through the impty MCP server** (`mcp__impty__start` / `send` /
  `send_signal` / `view` / `wait` / `snapshot` / `stop`) — a real pty with a faithful pyte
  render. Prefer `send` for Ctrl-C/ESC so you exercise imp's real tty paths rather than a
  backdoor signal; `send_signal(sig=…)` (SIGINT=2, SIGTERM=15) is the out-of-band path —
  used for SIGTERM and as the deterministic mid-run Ctrl-C (see the playbook's fidelity
  caveat). Full scroll-back is `view(full=True)`, not a separate tool.
- **Run in a throwaway standalone `git clone`** and abandon all churn at the end. It supplies
  cwd + a discardable imp project whose control root is *itself* — so a local-layer config
  override (`.imp/config.yaml`, e.g. to force a provider) both wins and stays isolated, which a
  linked worktree can't guarantee (its control root is the main checkout). Test against the
  installed `imp` like every other test — don't hard-code internal symbols or branch paths
  into a playbook.
- **Bug reports land in `docs/bugs/`** (in the main checkout, so they survive worktree
  deletion), named `<YYYY-MM-DD>-<feature>-playbook-results.md`. **A clean run is still a
  report** — write the matrix and an explicit "no defects observed" note. A passing run that
  produces no artifact is the most likely failure of this convention.

## Index

*(Append every new playbook here — this list is hand-maintained.)*

- `interrupt-playbook.md` — Ctrl-C / ESC / Ctrl-D clean-pause behavior across all four
  providers and several agent states. Validates `imp/specs/2026-06-04-esc-and-clean-interrupt.md`.
