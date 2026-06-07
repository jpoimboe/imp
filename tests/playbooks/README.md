# Playbooks — agent-driven acceptance tests

A **playbook** is a prose acceptance test for a feature, written for an *agent* (Claude)
to execute. Unlike the Python suite next door (`tests/`, `tests/impty/`), a playbook is
not collected by pytest: it is a self-contained instruction set that an agent follows to
drive the real `imp` and judge whether it behaves — and *speaks* — correctly.

## Conventions

- **One file per feature**, named `<feature>-playbook.md`. No date prefix: a playbook is a
  durable, re-runnable contract that tracks its feature across builds (unlike the dated
  specs/plans/runbooks under `docs/superpowers/`). It pairs 1:1 with a spec in
  `imp/specs/` — name it after the same feature so the pairing is obvious.
- **The agent drives `imp` through the impty MCP server** (`mcp__impty__start` / `send` /
  `view` / `wait` / `transcript` / `snapshot` / `stop`) — a real pty with a faithful pyte
  render. Every interrupt/keystroke goes through `send`; there is no out-of-band signal
  tool.
- **Run in a throwaway `git worktree`** and abandon all churn at the end. The worktree
  supplies cwd + a discardable imp project; test against the installed `imp` like every
  other test — don't hard-code internal symbols or branch paths into a playbook.
- **Bug reports land in `docs/bugs/`** (in the main checkout, so they survive worktree
  deletion), named `<YYYY-MM-DD>-<feature>-playbook-results.md`. A clean run is still a
  report — write the matrix and an explicit "no defects observed" note.

## Index

- `interrupt-playbook.md` — Ctrl-C / ESC / Ctrl-D clean-pause behavior across all four
  providers and several agent states. Validates `imp/specs/2026-06-04-esc-and-clean-interrupt.md`.
