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
        s.send("myproj\r")        # raw keystrokes; \r=Enter, \x1b[B=Down, \x03=Ctrl-C
    s.wait_idle()
    print(s.transcript())         # whole run in order (history + viewport)
    print(s.cell(0, 0).fg)        # style at a cell: 'cyan' (for color assertions)
```

## API

- `wait_idle(idle=0.2, hard=10.0) -> bool` — read until quiet; `False` on hard timeout.
- `send(bytes | str)` — raw keystrokes to the program.
- `viewport() -> str` — current visible screen (act on this each turn).
- `transcript(limit=None) -> str` — full run in order, bounded by `limit`.
- `cell(x, y) -> Cell` — glyph + style (`fg/bg/bold/...`) for appearance assertions.
- `cursor() -> (x, y)`, `is_alt_screen() -> bool`, `is_alive()`, `wait(timeout)`, `close()`.

By default the child inherits your environment with `TERM=xterm-256color` so a real
program's color emission (and thus `cell().fg`) is deterministic; pass `env=` to override.

Design + rationale: `_bmad-output/brainstorming/brainstorming-session-2026-06-05-1351.md`.
