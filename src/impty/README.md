# impty (provisional name)

Drive any interactive CLI/TUI on a real pty and read its faithfully-rendered
screen. Deterministic plumbing only — spawn, send raw keystrokes, read
viewport/transcript/cell snapshots. No semantic translation; an agent driver
and MCP adapter sit on top (not in this package yet).

```python
from impty import TerminalSession

with TerminalSession(["imp", "new", "myproj"], cols=80, rows=24) as s:
    s.wait_idle()                 # read until output goes quiet (select-based)
    if "Project name?" in s.viewport():
        s.send("myproj\r")        # raw keystrokes; \r=Enter, \x1b[B=Down
        # \x03 = Ctrl-C: a real SIGINT in cooked mode, a raw byte in raw mode;
        # use send_signal() for a mode-independent signal.
    s.wait_idle()
    print(s.transcript())         # whole run in order (history + viewport)
    print(s.cell(0, 0).fg)        # style at a cell: 'cyan' (for color assertions)
```

## API

- `wait_idle(idle=0.2, hard=10.0) -> bool` — read until quiet; `False` on hard timeout.
- `run_to_completion(hard=60.0) -> int` — drive the child to exit, draining ALL remaining output into pyte first; returns the exit code, raises `TimeoutExpired` on hard timeout (child still alive). Use this (not `wait()`) when the child floods after your last `send()`.
- `send(bytes | str)` — raw keystrokes to the program. `\x03` (Ctrl-C) is faithful: a real `SIGINT` to the child's process group in cooked mode (the controlling terminal is set up at spawn), a raw byte in raw mode (the app makes its own `KeyboardInterrupt`), exactly like a real terminal.
- `send_signal(sig=signal.SIGINT)` — deliver a real signal directly to the child's process group, regardless of tty mode (cooked or raw). The deterministic escape hatch when you don't want to depend on tty mode; a no-op once the child has exited. Reaches only the child's process group — a grandchild that `setsid()`'d into its own session is not signalled, exactly like a real Ctrl-C.
- `viewport() -> str` — current visible screen (act on this each turn).
- `transcript(limit=None) -> str` — full run in order, bounded by `limit`.
- `cell(x, y) -> Cell` — glyph + style (`fg/bg/bold/...`) for appearance assertions.
- `screen` (property) — the live pyte screen, for renderers that need the cell grid (see below).
- `cursor() -> (x, y)`, `is_alt_screen() -> bool`, `is_alive()`, `wait(timeout)`, `close()`.

By default the child inherits your environment with `TERM=xterm-256color` so a real
program's color emission (and thus `cell().fg`) is deterministic; pass `env=` to override.

## Seeing the UI (color images, not just text)

`view()`/`transcript()` return the grid as text. To actually *see* a TUI —
color, bold, box-drawing — render the pyte screen to a PNG and open it:

```python
from impty.render import render_screen
render_screen(session.screen, "out.png")   # true color/bold; faint via show_faint()
```

Two ready-made entry points:

- **Replay imp's recorded run journal → image** (no live program, no cost):
  `make snap-ui` writes `/tmp/imp_ui/stream.png` from the stored journal through
  imp's real renderer. `make snap-ui SNAP_ARGS="--limit 40"` for a slice, or
  `scripts/snap_imp_ui.py {stream,prompt} [--out PATH] [--open]` directly.
- **Drive a live program over MCP and screenshot it:** run the stdio MCP server
  (`python -m impty.mcp`, registered in `.mcp.json`). Tools: `start`, `send`,
  `wait`, `view`, `transcript`, `snapshot` (renders the live screen to a PNG and
  returns its path), `stop`. Adding/removing a tool needs a server restart.

Design + rationale: `_bmad-output/brainstorming/brainstorming-session-2026-06-05-1351.md`.
