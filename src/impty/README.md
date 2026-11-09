# impty (provisional name)

Drive any interactive CLI/TUI on a real pty and read its faithfully-rendered
screen. Deterministic plumbing only — spawn, send raw keystrokes, read
lines()/cell()/cursor() snapshots. No semantic translation; an agent driver
and MCP adapter sit on top (not in this package yet).

```python
from impty import TerminalSession

with TerminalSession(["imp", "new", "myproj"], cols=80, rows=24) as s:
    s.wait_idle()                 # read until output goes quiet (select-based)
    if "Project name?" in "\n".join(s.lines(limit=s.rows)):
        s.send("myproj\r")        # raw keystrokes; \r=Enter, \x1b[B=Down
        # \x03 = Ctrl-C: a real SIGINT in cooked mode, a raw byte in raw mode;
        # use send_signal() for a mode-independent signal.
    s.wait_idle()
    print("\n".join(s.lines()))   # whole run in order (scrollback + screen)
    print(s.cell(0, 0).fg)        # style at a cell: 'cyan' (for color assertions)
```

## API

- `wait_idle(idle=0.3, hard=3.0, *, since=0.0, predicate=None) -> bool` — read until quiet; `False` on hard timeout. `hard` is a max-patience ceiling (a settling program returns at `idle`) and is the whole wall-clock cap: every select slice is bounded by the remaining budget, so an `idle` window that cannot fit inside `hard` reports not settled at `hard` rather than settling after it. `since` is a monotonic stamp: only output read at or after it qualifies (the 0.0 default counts any output ever read), which is what the tool layer's `for_change` wait is built on. `predicate` is a zero-arg callable, checked up front and after every read that delivered bytes, that gates ONLY the quiet settle-return: while it is unfired the wait keeps going, so a program that went quiet before the awaited output arrived stays watched. EOF, child-exit and deadline are unchanged and the returned bool still means exactly "settled", so a caller learns whether it fired from the callable itself — this is what the tool layer's `for_text`/`pattern` wait is built on. Defaults are `DEFAULT_SETTLE_IDLE`/`DEFAULT_SETTLE_HARD`, the single source of truth the MCP tools inherit for settling; `DEFAULT_CHANGE_HARD` (30.0) is the change-mode ceiling the tool layer also inherits. The tool layer adds one policy of its own: it clamps each `wait`/`send`/`resize` call to `MAX_CALL_HARD` (240s) so no single MCP call blocks longer than that.
- `run_to_completion(hard=60.0) -> int` — drive the child to exit, draining ALL remaining output into pyte first; returns the exit code, raises `TimeoutExpired` on hard timeout (child still alive). Use this (not `wait()`) when the child floods after your last `send()`.
- `send(bytes | str)` — raw keystrokes to the program. `\x03` (Ctrl-C) is faithful: a real `SIGINT` to the terminal's foreground process group in cooked mode (the controlling terminal is set up at spawn), a raw byte in raw mode (the app makes its own `KeyboardInterrupt`), exactly like a real terminal.
- `send_signal(sig=signal.SIGINT)` — deliver a real signal to the terminal's FOREGROUND process group, regardless of tty mode (cooked or raw). The deterministic escape hatch when you don't want to depend on tty mode; a no-op once nothing holds the foreground. It is the aim a real Ctrl-C has, so under a shell doing its own job control it reaches the running job rather than the shell — and a grandchild that `setsid()`'d into its own session is not signalled. `close()` is deliberately separate: its SIGTERM→SIGKILL ladder always targets the direct child's own group, which always contains the child, so teardown can never be diverted by whoever holds the foreground.
- `lines(limit=None) -> list[str]` — rendered text rows over scrollback + the live screen. `lines(limit=rows)` is exactly the current screen, aligned row-for-row with `cell()`/`cursor()`; no limit returns the whole strip. The current screen is the thing to act on each turn.
- `cell(x, y) -> Cell` — glyph + style (`fg/bg/bold/...`) for appearance assertions.
- `screen` (property) — the live pyte screen, for renderers that need the cell grid (see below).
- `returncode -> int | None` (property) — the child's exit status once it has exited, else `None`. Raw `Popen` semantics: negative for a signal death (`-2` for SIGINT). The tool layer names the signal; this one stays raw.
- `close(grace=1.0) -> int` — end the session and return the child's raw exit code (negative for a signal death). SIGTERMs the child's process group, polls up to `grace` seconds for it to exit — draining its cleanup output into pyte meanwhile — then SIGKILLs, and drains whatever it left buffered before closing the master. `grace=0.0` skips SIGTERM entirely. Idempotent: a second call re-returns the same code.
- `cursor() -> (x, y)`, `regime() -> Regime`, `is_alt_screen() -> bool`, `rows`/`cols` (properties), `resize(rows, cols)`, `is_alive()`, `wait(timeout)`.

By default the child inherits your environment with `TERM=xterm-256color` so a real
program's color emission (and thus `cell().fg`) is deterministic. At the
`TerminalSession` layer an explicit `env=` IS the child environment (plus that `TERM`
default) — composing it is the caller's job, deliberately, so an embedding host's
curated child env can never be polluted with `os.environ`. `ImptyDriver.start` does
that composition for you: its `env` entries are ADDED to the base environment,
overriding same-named ones.

## Embedding: the sandbox hook

impty imports nothing from its host and never requires a sandbox. A host can inject
both callables:

```python
ImptyDriver(sandbox=hook, cwd_resolver=resolve_cwd)
# hook(argv, cwd, env) -> PtySpawn | None
# resolve_cwd(cwd) -> transformed cwd
```

The optional `ImptyDriver(cwd_resolver=...)` hook runs on every `start()` before the
sandbox hook or bare spawn is selected, and the transformed cwd is passed to either
route. Without a cwd resolver, omitted, relative, and absolute cwd values retain the
host process's normal `Popen` handling.

Returning `PtySpawn(argv, env, pass_fds, openpty)` replaces the spawn wholesale: the
host's own argv, the child environment it computed, the fds that must survive the fork
— which `TerminalSession(pass_fds=…)` forwards to `Popen` — and, optionally, the host's
own pty allocator, for a host that needs the pty to come from somewhere other than the
caller's devpts; it is forwarded as `TerminalSession(openpty=…)` and defaults to
`pty.openpty`. The transformed cwd is host-resolved inside `plan.argv`, so the session
is spawned with `cwd=None` (`Popen`'s `cwd=` would resolve in the *caller's* mount
namespace). Returning `None` — or passing no hook at all, as `python -m impty.mcp`
does — spawns the program bare.

## The pty journal (evidence that outlives the screen)

pyte keeps only the live screen plus `history` rows of scrollback — and none at all
in the alt-screen regime — so what a program actually emitted is gone the moment it
scrolls past. A host that wants that evidence passes a directory:

```python
ImptyDriver(journal_dir=step_dir, snapshot_dir=quest_home / "snapshots")
TerminalSession(argv, journal=Journal(step_dir, 1))   # the layer underneath
```

Per session, numbered by a per-driver counter (a step that start/stop/starts writes
`pty-1.*` then `pty-2.*`):

```
pty-1.raw      exactly the bytes read off the pty master
pty-1.raw.1    the rotated predecessor
pty-1.jsonl    the sidecar
```

Both files are opened unbuffered, so a chunk reaches the fd on the read that produced
it and the journal survives a `SIGKILL` of the host process — the wedged-daemon case
where it is worth most. Any `OSError`, the initial `open()` included, disables that
session's journal and logs one warning; it never propagates into the drive. With no
`journal_dir` (the standalone `python -m impty.mcp` server) nothing is written.

**The recording boundary is the master read, never the child's `write()`.** `.raw`
holds what `os.read` returned from the master — what a real terminal would have
received — and the slave's line discipline sits in between, which impty never
reconfigures. So for a cooked-mode child `.raw` legitimately contains bytes the
program never wrote:

- **echo** — keystrokes sent with `send()` reappear on the read side and are
  journaled like any other output;
- **`ONLCR`** — every `\n` the child writes is delivered, and recorded, as `\r\n`.

A TUI that puts its tty in raw mode does neither, which is the common case for the
drives this exists for.

Purity is a property of the *writer*: the single write path into `.raw` is the one
`chunk` variable in `TerminalSession._read_step`, so nothing the journal itself adds
— framing, markers, a copy of sent keystrokes, pyte's CPR/DA replies — can appear
there. That is what makes `cat pty-1.raw` in a real terminal replay the drive in full
color with zero tooling, and the line discipline's transformations are *why* it
replays, since a real terminal expects the `\r\n` form. There is no reader in this
package by design: `cat` reads the `.raw`, and every sidecar line is plain JSON. A
recording bakes in its record-time width (wrapped lines, absolute cursor
addressing), so it is faithful at the geometry it was recorded at and nowhere else.
Anything that would violate purity belongs in the sidecar.

### The sidecar

`pty-N.jsonl`, one JSON object per line. Every record carries `t` (seconds since
session start, 3 decimals), `off` (the logical byte offset into the whole output
stream — it keeps counting across rotation, so it names a position even in bytes that
are no longer on disk) and `ev`:

```json
{"t":0.0,"off":0,"ev":"start","argv":["imp","list"],"cols":80,"rows":24}
{"t":1.234,"off":4096,"ev":"resize","cols":120,"rows":40}
{"t":2.5,"off":8192,"ev":"send","len":1}
{"t":7.75,"off":4210688,"ev":"rotate","dropped":0}
{"t":15.5,"off":8409088,"ev":"rotate","dropped":4210688}
{"t":19.9,"off":8450000,"ev":"end","exit":0}
```

- `start` — the argv as spawned and the initial geometry, written at construction.
- `resize` — the new geometry, on every `resize()`. Load-bearing: SIGWINCH is out of
  band, so a resize leaves no trace whatever in the byte stream.
- `send` — `len` only, never the content. It exists to segment turns, and the typed
  bytes are already in the caller's own transcript. Not a secrecy boundary: under
  cooked-mode echo those same keystrokes are in `.raw`.
- `rotate` — one per rollover (see below).
- `end` — the child's raw exit code, negative for a signal death, written once on the
  first `close()`; `close()` is idempotent and so is the record.

### Rotation

`.raw` is bounded tail-first. A chunk is always written whole — it is the atomic unit
of what was read — and only *after* it lands is the segment measured: at 4 MB or more
`pty-N.raw` is renamed to `pty-N.raw.1`, replacing any prior one, and a fresh `.raw`
is opened. A segment therefore lands in `[4 MB, 4 MB + 65535]` and the retained bound
is `2 x (4 MB + 64 KiB)` — the whole-chunk overshoot, not a flat 8 MB. A cap that
stopped recording would discard the end of the stream, which is exactly where a
failure is.

`dropped` is the cumulative size of the segments no longer reachable in any retained
file. A rotation discards a segment only by overwriting the previous `.raw.1`, so the
**first** rotation reports `"dropped":0` — both segments are still on disk — the
second reports the size of the first segment, and so on. The sidecar does not rotate:
it gets one record per tool call.

### Where imp puts it

imp passes the step transcript dir, `<quest_home>/steps/<phase>/<step>/`, so the
journal sits beside that step's `stderr.log`: never committed (only `artifacts/` is
harvested into the quest's artifact ref), reaped with the quest, archived only for a
conversation you mean to resume, and masked from both the sandboxed program and any
agent's `read_file` — the right containment for raw bytes that may carry a token.

`snapshot()`'s default PNG goes to `<quest_home>/snapshots/`, created by the driver,
instead of accumulating in the system temp dir forever; it stays readable through the
agent's `read_file`, an explicit `snapshot(path=...)` is unaffected, and with no
`snapshot_dir` the default is a temp file exactly as before. Both directories live
under the quest home rather than `/tmp`, so the sandbox's read-only `/tmp` — only the
per-quest scratch dir is bound writable — leaves them where they are.

## Sequences pyte can't parse (the `TerminalStream` filter)

Child output goes through `impty.stream.TerminalStream` before pyte's parser. It
drops exactly three classes of sequence whole, and nothing else:

- **Private-prefixed CSI** — `ESC [` followed by `<`, `=` or `>`: secondary DA
  (`ESC[>c`), XTVERSION, XTMODKEYS (`ESC[>4;2m`), the kitty keyboard protocol.
  pyte gets these wrong two different ways. It swallows a `>` and dispatches the
  sequence as if unprefixed, so a secondary DA answers with a surplus primary-DA
  reply the program never reads — it comes back as *keystrokes* and is typed into
  the pane — and XTMODKEYS lands as SGR `4;2`, underlining everything after it.
  `<` and `=` it treats as *final* bytes, so the rest of the sequence is drawn as
  screen text (`ESC[=5u` leaves a stray `5u`).
- **Intermediate-byte CSI** — a CSI carrying an intermediate byte (0x20–0x2F):
  DECSTR (`ESC[!p`, sent by `reset`), DECSCL (`ESC[1"p`), SL (`ESC[2 @`), DECSCUSR
  (`ESC[2 q`), and `?`-prefixed forms such as DECRQM (`ESC[?2026$p`). pyte takes
  the intermediate for the final, so the real final is drawn (a stray `p`) or run
  as another command (SL as ICH).
- **String sequences** — DCS (`ESC P`), SOS (`ESC X`), PM (`ESC ^`), APC
  (`ESC _`), through their `ESC \` or BEL terminator. pyte does not implement
  them and draws the payload as screen text (a kitty-graphics app splatters
  base64 across the grid).

OSC (`ESC ]`) and `?`-prefixed CSI without an intermediate byte are deliberately
left to pyte — `?` carries the DECSET alt-screen switch `regime()` depends on, and
`TerminalScreen` guards the few `?` forms pyte mis-dispatches (below). The filter
is a byte-level state machine, not a regex, so it is safe across chunk boundaries
and holds only `ESC`, or `ESC [` plus the parameter bytes of the CSI in progress.

Nothing is answered in a dropped sequence's place. A query impty does not
genuinely emulate gets silence, never a fabricated reply, so a program falls back
to conservative defaults instead of being told about capabilities impty cannot
render; `TerminalScreen` follows the same rule for private device-status queries
(`ESC[?996n`, `ESC[?6n`), and ignores a multi-parameter device-attributes
response echoed back by a raw-mode child rather than crashing on its arity, while
plain CPR (`ESC[6n`) still gets its real answer. A private-parameter SGR (`ESC[?4m`,
which vim emits at startup) is dropped for the same reason: pyte's SGR handler takes
no `private` argument, and the resulting error used to cost the whole read chunk. If
a sequence still trips pyte,
the chunk is dropped and logged rather than propagating out of a read — a
degraded screen, not a dead session.

## Seeing the UI (color images, not just text)

`lines()` and the MCP `view` tool return the grid as text. To actually *see* a TUI —
color, bold, box-drawing — render the pyte screen to a PNG and open it:

```python
from impty.render import render_screen
render_screen(session.screen, "out.png")   # true color/bold; faint via show_faint()
```

Two ready-made entry points:

- **Replay imp's recorded run journal → image** (no live program, no cost):
  `make snap-ui` writes `/tmp/imp_ui/stream.png` from the stored journal through
  imp's real renderer. `make snap-ui SNAP_ARGS="--limit 40"` for a slice, or
  `scripts/snap_imp_ui.py {stream,stream-live,prompt} [--out PATH] [--open]`
  directly; `stream-live` drives the scene on a real pty so the live status bar
  is in the capture.
- **Drive a live program over MCP and screenshot it:** run the stdio MCP server
  (`python -m impty.mcp`, registered in `.mcp.json`). `start` waits until it settles,
  exits, or the 3s settle deadline elapses, then returns `[full screen]` followed by
  the complete screen accumulated so far; for slow startup use `wait(hard=...)` then
  `view()`. `send` takes `data=` literal text and/or `keys=["enter"]` named keys,
  resolved and delivered in one write, and its `for_text=`/`pattern=` waits for the
  reaction text instead of a settle. `send(wait=False)` returns `[sent]` without
  rendering, and `resize(..., wait=False)` returns `[resized]` without rendering;
  otherwise `send`, `resize`, `wait`, and `send_signal` return a changed-row diff with
  0-based labels, `[no text change]`, or `[full screen]` plus the complete screen.
  A matcher reply is the same diff shape under a `[found]` / `[not seen …]` header.
  `view` returns the complete current screen with no diff or `[full screen]` marker
  and resynchronizes subsequent changed-row responses; `full=True` returns the whole
  strip and, once the child is gone, its header carries the exit status — `exit=0`,
  `exit=3`, or `exit=SIGINT`. `send_signal` delivers a real signal to the terminal's
  foreground process group (e.g. `sig=2` for mode-independent Ctrl-C); `resize`
  (`rows, cols`) delivers a real `SIGWINCH`; `wait(for_change=True)` waits for NEW
  output, and its `for_text=`/`pattern=` blocks until that text is on the current
  screen, heading the reply `[found]` or `[not seen …]`. `snapshot` renders the live
  screen to a PNG and returns its path; `stop` sends SIGTERM, allows a 1s grace, then
  SIGKILL, and returns the program's exit status and final screen. Adding/removing a
  tool needs a server restart.
