# Driving terminal UIs

## Overview

Some work changes how a program behaves *on a terminal* — its on-screen output,
a full-screen TUI, or an interactive prompt. You can't judge that from the source
alone. Drive the real program on a pseudo-terminal and look at what it renders.

You hold the impty tools (`mcp__impty__*`); their descriptions are one-liners and
this skill is their manual. They share one live terminal session across your turns,
so you can spawn a program, watch it, type into it, and watch it react.

## When to use

- A change to a CLI's output, colors, or layout.
- A full-screen TUI (menus, tables, status lines, cursor movement).
- An interactive prompt or wizard that reacts to keystrokes.
- Any "does it actually look/behave right?" only running it can answer.

For a program that prints and exits, one `start` + `snapshot` is enough. Reserve
the send/react loop for interactive programs.

## The tools

- **`start(argv, cwd=None, cols=80, rows=24, env=None)`** — spawn `argv` on a fresh
  pty; returns the first screen. `cols`/`rows` set the size real programs reflow to.
  `env`, if given, *replaces* the environment (TERM still defaulted). Replaces any
  existing session.
- **`view(full=False)`** — read the current screen as text, sending nothing. A cursor
  parked after a prompt means it's waiting for input. `full=True` adds history.
- **`send(data, wait=True)`** — raw keystrokes: Enter=`\r`, Tab=`\t`, Ctrl-C=`\x03`,
  Backspace=`\x7f`, arrows=`\x1b[A`/`B`/`C`/`D`. `wait=True` settles then returns the
  screen; `wait=<seconds>` settles a fixed time (for an always-animating TUI);
  `wait=False` returns immediately.
- **`snapshot(path=None, full=False, light=False)`** — render the screen to a PNG and
  return its path; **read it with `read_file` to see color, bold, and box-drawing**
  (`view` is text only). Let `path` default to a temp file — don't point it into the
  worktree. `light=True` previews a light theme.
- **`wait(idle=…, hard=…)`** — let a still-working program settle before you look.
- **`resize(rows, cols, wait=True)`** — resize the terminal (SIGWINCH) to test reflow.
- **`send_signal(sig=2)`** — deliver a real signal to the program's group (SIGINT=2,
  SIGTERM=15), independent of tty mode.
- **`stop()`** — kill the current program and end the session.

## The loop

1. `start` the program at the size you're testing.
2. `snapshot` (+ `read_file`) for the look, or `view` for quick structure.
3. Decide the next keystroke from what you saw; `send` it.
4. Repeat; `stop` when done.

The session is stateful and persists across your tool calls — don't respawn between
steps. `snapshot`'s PNG goes to a temp dir, so it works even where the worktree is
read-only.
