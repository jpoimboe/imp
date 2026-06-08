"""A minimal stdio MCP server that gives an agent eyes + fingers on one
interactive program through an impty `TerminalSession`.

This is the Fork-B *spike* (brainstorming 2026-06-05-1804): the smallest thing
that lets Claude Code drive `imp` (then any program) over a real pty and read
its faithfully-rendered screen — manually, for play. Deliberately omits the
deferred machinery: no `.md` test format, no orchestrator/runner, no verdict
tool, no deterministic anchors, no multi-session lifecycle. One lazy session;
point it at a program with `start`, drive with `send`/`view`, end with `stop`.

No semantic translation: read tools return the rendered grid text (what pyte
drew), `send` writes raw keystroke bytes. MCP is just the pipe.

Run (stdio): `python -m impty.mcp`. Register it for Claude Code via `.mcp.json`.

The library renders all three regimes (append-only, inline-redraw, alt-screen)
faithfully: alt-screen apps get a real buffer swap and `view` shows the live
frame; `regime` in the status header reports which shape the session is in.
"""

from __future__ import annotations

import codecs
import re
import signal
import time

from mcp.server.fastmcp import FastMCP

from impty import TerminalSession

mcp = FastMCP("impty")

# The settle policy shared by start/send/send_signal/wait: how long the program
# must be quiet to count as "done reacting" (idle) and the wall-clock ceiling
# before we give up and return whatever's on screen (hard). One place, so the
# four call sites can't drift apart.
_SETTLE_IDLE = 0.3
_SETTLE_HARD = 15.0

# C-style escape mnemonics the send() contract documents (\r, \t, \x03, \x1b…).
# Only RECOGNIZED escapes are matched, so ordinary text — including non-ASCII
# and lone backslashes — passes through untouched.
_ESCAPE_RE = re.compile(
    r"\\(?:x[0-9a-fA-F]{2}|[0-7]{1,3}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|[\\'\"abfnrtv0])"
)


def _decode_keys(data: str) -> str:
    r"""Decode the keystroke escape mnemonics send() documents (``\r``, ``\t``,
    ``\x03``, ``\x1b[B`` …) into the real control bytes they name.

    The agent→MCP JSON boundary delivers a written ``\r`` as the two literal
    characters backslash + 'r', not a carriage return — so without this the
    documented mnemonics never reach the program and Enter can't submit a
    prompt. Each matched token is pure ASCII, so unicode_escape is safe; real
    Unicode and unrecognized lone backslashes are left exactly as typed."""
    return _ESCAPE_RE.sub(lambda m: codecs.decode(m.group(0), "unicode_escape"), data)

# One lazy session at a time. `start` replaces any existing one. This is the
# most playable default, NOT a commitment to the deferred lifecycle question.
_session: TerminalSession | None = None


def _require() -> TerminalSession:
    if _session is None:
        raise RuntimeError("no session — call start(argv=[...]) first")
    return _session


def _render(session: TerminalSession, *, full: bool = False) -> str:
    """A one-line status header plus the screen text. Default body is the CURRENT
    screen window (lines(limit=rows)) — NOT the whole strip — since these tools
    are called constantly while driving and a multi-hundred-row dump is noise.
    full=True returns the whole history+screen strip. The cursor is screen-local
    and bounded, so a small y near the bottom is the cheap 'parked = waiting for
    input' tell."""
    with session.paused():  # one consistent frame: header + body from the same pyte state
        cx, cy = session.cursor()
        status = (
            f"[alive={session.is_alive()} regime={session.regime().value} "
            f"cursor=({cx},{cy})]"
        )
        body = session.lines() if full else session.lines(limit=session.rows)
    return f"{status}\n" + "\n".join(body)


@mcp.tool()
def start(
    argv: list[str],
    cwd: str | None = None,
    cols: int = 80,
    rows: int = 24,
) -> str:
    """Spawn an interactive program on a fresh pty and return its first screen.

    Replaces any existing session. `argv` is the command + args, e.g.
    ["imp", "new", "my-change"]. `cols`/`rows` set the terminal size (a real
    test parameter — programs reflow by width). Waits for the program to settle
    before returning, so the returned screen is the first thing to react to.
    """
    global _session
    if _session is not None:
        _session.close()
        _session = None
    session = TerminalSession(argv, cwd=cwd, cols=cols, rows=rows)
    session.wait_idle(idle=_SETTLE_IDLE, hard=_SETTLE_HARD)
    _session = session
    return _render(session)


@mcp.tool()
def view(full: bool = False) -> str:
    """Read the screen without sending anything.

    By default returns the CURRENT screen (what a human sees right now); the
    status header reports liveness, regime (scroll|screen), and cursor position
    (a cursor parked just after a prompt = waiting for input). Set full=True to
    get the whole run so far (scrolled-off history + the current screen) — a
    potentially long dump, so it is opt-in.
    """
    return _render(_require(), full=full)


@mcp.tool()
def send(data: str, wait: bool = True) -> str:
    """Send raw keystrokes to the program, then (by default) return the new screen.

    `data` is literal bytes the program receives — NOT a description. Common keys:
    Enter=`\\r`, Tab=`\\t`, Ctrl-C=`\\x03`, Backspace=`\\x7f`,
    Up=`\\x1b[A` Down=`\\x1b[B` Right=`\\x1b[C` Left=`\\x1b[D`.
    So answering a prompt "foo" then Enter = data="foo\\r".

    With wait=True (default) it waits for the program to react and returns the
    fresh screen — type-then-look in one call. Set wait=False to fire keys
    without waiting (e.g. mid-stream input).
    """
    session = _require()
    # Mark the type-then-look boundary just before the keystroke so wait_idle
    # settles on the REACTION, not on a stale pre-send screen the drainer last
    # touched seconds ago.
    since = time.monotonic()
    session.send(_decode_keys(data))
    if not wait:
        return "[sent]"
    session.wait_idle(idle=_SETTLE_IDLE, hard=_SETTLE_HARD, since=since)
    return _render(session)


@mcp.tool()
def send_signal(sig: int = signal.SIGINT) -> str:
    """Deliver a real signal to the program's process group, then return the new
    screen. The mode-INDEPENDENT interrupt: unlike `send("\\x03")` — which only
    becomes a SIGINT when the tty's line discipline has ISIG on (cooked/cbreak)
    and is just a raw byte at a prompt_toolkit prompt — this bypasses the tty
    entirely. Use it when you want a deterministic Ctrl-C regardless of tty mode,
    instead of scraping the pid and shelling out to `kill`.

    `sig` is the signal number: SIGINT=2 (Ctrl-C, default), SIGTERM=15. Reaches
    only the child's process group — a grandchild that `setsid()`'d into its own
    session is not signalled, exactly like a real Ctrl-C. A no-op once the child
    has exited.
    """
    session = _require()
    # Same type-then-look boundary as send(): settle on the signal's reaction
    # (handler output, exit), not on a stale pre-signal screen.
    since = time.monotonic()
    session.send_signal(sig)
    session.wait_idle(idle=_SETTLE_IDLE, hard=_SETTLE_HARD, since=since)
    return _render(session)


@mcp.tool()
def wait(idle: float = _SETTLE_IDLE, hard: float = _SETTLE_HARD) -> str:
    """Wait for the program to produce output and go quiet, then return the screen.

    For watching a program that's still working (spinners, streaming) without
    sending input. `idle` = quiet window that counts as settled; `hard` = max
    wall-clock before giving up.
    """
    settled = _require().wait_idle(idle=idle, hard=hard)
    header = "[settled]" if settled else "[still busy after hard cap]"
    return f"{header}\n{_render(_require())}"


@mcp.tool()
def snapshot(path: str | None = None, full: bool = False) -> str:
    """Render the screen to a PNG (true color, bold weight, box-drawing glyphs)
    and return its path — for SEEING the TUI, not just the grid text view()
    returns. The image reflects the live session's size.

    Renders the current frame by default; full=True (scroll shape) also paints
    scrolled-off history above it — a tall image, so it is opt-in. `path`
    defaults to a fresh temp file. Faint (dim) text isn't shown — pyte discards
    SGR 2 at parse time — but color, bold, and layout are faithful.
    """
    import os
    import tempfile

    from impty.render import render_screen

    session = _require()
    if path is None:
        fd, path = tempfile.mkstemp(prefix="impty-", suffix=".png")
        os.close(fd)
    with session.paused():  # freeze pyte while render reads the raw screen buffer
        render_screen(session.screen, path, full=full)
    return f"[wrote {path}]"


@mcp.tool()
def stop() -> str:
    """Kill the current program and end the session."""
    global _session
    if _session is None:
        return "[no session]"
    _session.close()
    _session = None
    return "[stopped]"


def main() -> None:
    mcp.run()  # stdio transport by default


if __name__ == "__main__":
    main()
