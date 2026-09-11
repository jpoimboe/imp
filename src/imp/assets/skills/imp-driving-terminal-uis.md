# Driving terminal UIs

## Overview

Some work changes how a program behaves *on a terminal* — its on-screen output,
a full-screen TUI, or an interactive prompt. You can't judge that from source
alone; drive the real program on a pty and look at what it renders.

You hold the impty tools (`mcp__impty__*`); their descriptions are one-liners and
this skill is their manual. They share one live terminal session across your turns
— don't respawn between steps.

## When to use

- A change to a CLI's output, colors, or layout.
- A full-screen TUI (menus, tables, status lines, cursor movement).
- An interactive prompt or wizard that reacts to keystrokes.
- Any "does it actually look/behave right?" only running it can answer.

For print-and-exit programs, one `start` + `snapshot` is enough. Reserve the
send/react loop for interactive programs.

## The tools

Diff hunks use 0-based labels such as `[row 7]`, `[rows 20-22 inserted]`, and
`[3 rows deleted at row 0]`. Call `view()` to resynchronize.

- **`start(argv, cwd=None, cols=80, rows=24, env=None)`** — spawn `argv` on a fresh
  pty; after it settles, exits, or the 3s settle deadline elapses, return
  `[full screen]` followed by the complete screen accumulated so far. The startup
  screen can be incomplete; for slow startup call `wait(hard=...)` then `view()`.
  `cwd` defaults to the project root. Relative values are resolved from that root;
  absolute values are used as written. `cols`/`rows` set the size real programs
  reflow to. `env` entries are ADDED to the environment, overriding same-named ones (TERM
  still defaulted). Refuses while a program is still running — stop() first.
- **`view(full=False)`** — read the complete current screen without a diff or
  `[full screen]` marker; resynchronizes subsequent changed-row responses. A cursor
  parked after a prompt means it's waiting for input. `full=True` adds history. Once
  the program has exited the header carries its exit status: `exit=0`, `exit=2`, or a
  signal name like `exit=SIGINT`.
- **`send(data="", keys=None, wait=True, for_text=None, pattern=None)`** — `data` is
  literal text, escapes decoded: Enter=`\r`, Tab=`\t`, Ctrl-C=`\x03`. `keys` is a list
  of key NAMES, sent after it in one write: enter, tab, shift-tab, escape, space,
  backspace, delete, insert, up, down, left, right, home, end, pageup, pagedown,
  f1-f12, ctrl-<a-z>, alt-<key> (`ctrl-c`, `Ctrl+C`, `CTRL_C` are one key). Prefer
  `keys` for non-printing keys — a mistyped escape in `data` is silently typed into
  the program, a mistyped NAME is an error that sends nothing; "foo" then Enter is
  `data="foo", keys=["enter"]`. `wait=False` returns `[sent]` without rendering;
  otherwise return a changed-row diff, `[no text change]`, or `[full screen]` followed
  by the complete screen. `wait=True` settles; `wait=<seconds>` settles a fixed time
  for an always-animating TUI; `for_text=`/`pattern=` waits for that text as `wait()`
  does and returns its header (needs the default `wait=True`).
- **`snapshot(path=None, full=False, light=False)`** — render the screen to a PNG and
  return its path; **read it with `read_file` to see color, bold, and box-drawing**
  (`view` is text only). Let `path` default to a temp file — don't point it into the
  worktree. `light=True` previews a light theme.
- **`wait(idle=…, hard=…, for_change=False, for_text=None, pattern=None)`** — wait
  until there is an idle window after qualifying output, the process exits, or `hard`
  elapses; then return a changed-row diff, `[no text change]`, or `[full screen]`
  followed by the complete screen; already-read output qualifies normally;
  `for_change=True` requires new output, to follow a long command without polling. A
  live process with no output is not settled. `for_text=` (substring) or `pattern=`
  (regex, anchored per screen line) instead blocks until that text appears — waiting
  for a named prompt beats polling `view` — and heads the reply `[found]`,
  `[not seen after …]`, or `[not seen — program exited]`. It scans the CURRENT screen,
  so pick a string unique to the reaction you expect, and keep it short: one spanning
  a line the terminal wrapped never matches. `hard` defaults to 3s (30s with
  `for_change=True` or a matcher), caller values are accepted, and values over 240s
  are capped; call again to keep watching.
- **`resize(rows, cols, wait=True)`** — resize the terminal (SIGWINCH) to test reflow.
  `wait=False` returns `[resized]` without rendering; otherwise return a changed-row
  diff, `[no text change]`, or `[full screen]` followed by the complete screen.
  `wait=True` settles; `wait=<seconds>` settles a fixed time and values over 240s are
  capped.
- **`send_signal(sig=2)`** — deliver a real signal to the terminal's foreground
  process group (SIGINT=2, SIGTERM=15), independent of tty mode — in a shell it
  reaches the running job, not the shell. Then return a changed-row diff,
  `[no text change]`, or `[full screen]` followed by the complete screen.
- **`stop()`** — end the session: SIGTERM the program's group, allow it 1s to clean
  up, then SIGKILL. Returns its exit status and final screen. `send_signal(9)` first
  if you want it killed outright.

## The loop

1. `start` the program at the size you're testing.
2. `snapshot` (+ `read_file`) for the look, or `view` for quick structure.
3. Decide the next keystroke from what you saw; `send` it.
4. Repeat; `stop` when done.

Snapshots work even where the worktree is read-only.

The program runs in the same sandbox as your `bash` calls and shares their pid
namespace: `ps` in a bash call sees it and can kill it, masked paths stay masked,
and everything it spawns dies when you finish. `create_and_enter_worktree`
rebuilds that sandbox and so ends the session — `start` again after it.

## Showing the human

Paste the screen into your message as a fenced block: color survives there. You
cannot type the ESC byte in prose, so write each SGR code without it — `[1;33m`,
`[0m` — and imp restores it inside the fence, and inside a single-line inline
code span, so a short sample like `` `[1;33m78%[0m` `` keeps its color
mid-sentence. Nowhere else is anything restored, and any control byte that isn't
SGR arrives as literal `^[[33m`. When the look matters beyond color, give the
snapshot PNG path too.
