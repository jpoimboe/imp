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

Caveat: the library only *detects* alt-screen today (buffer-swap fidelity is a
later slice), so `view` is faithful for an append-only CLI like imp but degraded
for a full alt-screen TUI like claude code.
"""

from __future__ import annotations

import codecs
import re

from mcp.server.fastmcp import FastMCP

from impty import TerminalSession

mcp = FastMCP("impty")

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


def _render(session: TerminalSession) -> str:
    """The current screen plus a one-line status header the agent can trust."""
    cx, cy = session.cursor()
    status = (
        f"[alive={session.is_alive()} alt_screen={session.is_alt_screen()} "
        f"cursor=({cx},{cy})]"
    )
    return f"{status}\n{session.viewport()}"


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
    session.wait_idle(idle=0.3, hard=15.0)
    _session = session
    return _render(session)


@mcp.tool()
def view() -> str:
    """Read the current screen (viewport) without sending anything.

    Use to re-read after `wait`, or to look again. Each row is what a human
    would see right now; the status header reports liveness, alt-screen, and
    cursor position (a cursor parked just after a prompt = waiting for input).
    """
    return _render(_require())


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
    session.send(_decode_keys(data))
    if not wait:
        return "[sent]"
    session.wait_idle(idle=0.3, hard=15.0)
    return _render(session)


@mcp.tool()
def wait(idle: float = 0.3, hard: float = 15.0) -> str:
    """Wait for the program to produce output and go quiet, then return the screen.

    For watching a program that's still working (spinners, streaming) without
    sending input. `idle` = quiet window that counts as settled; `hard` = max
    wall-clock before giving up.
    """
    settled = _require().wait_idle(idle=idle, hard=hard)
    header = "[settled]" if settled else "[still busy after hard cap]"
    return f"{header}\n{_render(_require())}"


@mcp.tool()
def snapshot(path: str | None = None) -> str:
    """Render the CURRENT screen to a PNG (true color, bold weight, box-drawing
    glyphs) and return its path — for SEEING the TUI, not just the grid text that
    view()/transcript() return. The image reflects the live session's size.

    `path` defaults to a fresh temp file (read it back to view the image). Faint
    (dim) text isn't shown — pyte discards SGR 2 at parse time — but color, bold,
    and layout are faithful.
    """
    import os
    import tempfile

    from impty.render import render_screen

    session = _require()
    if path is None:
        fd, path = tempfile.mkstemp(prefix="impty-", suffix=".png")
        os.close(fd)
    render_screen(session.screen, path)
    return f"[wrote {path}]"


@mcp.tool()
def transcript(limit: int | None = None) -> str:
    """The whole run so far in order (scrolled-off history + current screen).

    For reviewing everything that happened, not just the visible screen. `limit`
    keeps only the last N lines (bounded — never an unbounded dump). Note: only
    captures output the server has read; drive with `send`/`wait` to keep it
    current. (For an append-only CLI this is the full scroll; in alt-screen mode
    it is largely N/A.)
    """
    return _require().transcript(limit)


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
