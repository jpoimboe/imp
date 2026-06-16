"""Transport-neutral driver for one interactive program over an impty
`TerminalSession`. Owns the session and exposes the seven tool bodies (start,
view, send, send_signal, wait, snapshot, stop) plus helpers. The tool
descriptions live on the method docstrings as the single source of truth — the
provider host adapters (claude_code SDK tools, codex FastMCP, the standalone
stdio server) all surface these same methods, so there is no per-provider
description drift. Calls are blocking (wait_idle, time.sleep, pty reads); each
in-process host offloads them to a thread.
"""

from __future__ import annotations

import codecs
import re
import signal
import time
from collections.abc import Callable

from impty import TerminalSession
from impty.session import DEFAULT_SETTLE_HARD, DEFAULT_SETTLE_IDLE

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


class ImptyDriver:
    """One lazy `TerminalSession` at a time. `start` replaces any existing one."""

    def __init__(self) -> None:
        self._session: TerminalSession | None = None

    def _require(self) -> TerminalSession:
        if self._session is None:
            raise RuntimeError("no session — call start(argv=[...]) first")
        return self._session

    def _render(self, session: TerminalSession, *, full: bool = False) -> str:
        """A one-line status header plus the screen text. Default body is the
        CURRENT screen window (lines(limit=rows)) — NOT the whole strip — since
        these tools are called constantly while driving. full=True returns the
        whole history+screen strip. The cursor is screen-local and bounded, so a
        small y near the bottom is the cheap 'parked = waiting for input' tell."""
        with session.paused():
            cx, cy = session.cursor()
            status = (
                f"[alive={session.is_alive()} regime={session.regime().value} "
                f"cursor=({cx},{cy})]"
            )
            body = session.lines() if full else session.lines(limit=session.rows)
        return f"{status}\n" + "\n".join(body)

    def start(
        self,
        argv: list[str],
        cwd: str | None = None,
        cols: int = 80,
        rows: int = 24,
        env: dict[str, str] | None = None,
    ) -> str:
        """Spawn an interactive program on a fresh pty and return its first screen.

        Replaces any existing session. `argv` is the command + args, e.g.
        ["imp", "new", "my-change"] — spawned directly, so the program is the
        foreground, controlling-tty session leader (Ctrl-C/ESC reach it). No shell
        wrapper is needed; pass `env` to set variables (e.g. API keys) instead of a
        `bash -lc "export …; exec …"` wrapper, which would otherwise put bash — not
        your program — in the foreground group. `env`, when given, REPLACES the
        inherited environment (TERM is still defaulted); merge with the parent env
        yourself if you want to extend it. `cols`/`rows` set the terminal size (a
        real test parameter — programs reflow by width). Waits for the program to
        settle before returning, so the returned screen is the first thing to react to.
        """
        if self._session is not None:
            self._session.close()
            self._session = None
        session = TerminalSession(argv, cwd=cwd, cols=cols, rows=rows, env=env)
        try:
            session.wait_idle()  # inherit the library settle policy
        except BaseException:
            session.close()
            raise
        self._session = session
        return self._render(session)

    def view(self, full: bool = False) -> str:
        """Read the screen without sending anything.

        By default returns the CURRENT screen (what a human sees right now); the
        status header reports liveness, regime (scroll|screen), and cursor position
        (a cursor parked just after a prompt = waiting for input). Set full=True to
        get the whole run so far (scrolled-off history + the current screen) — a
        potentially long dump, so it is opt-in.
        """
        return self._render(self._require(), full=full)

    def send(self, data: str, wait: bool | float = True) -> str:
        """Send raw keystrokes to the program, then (by default) return the new screen.

        `data` is literal bytes the program receives — NOT a description. Common keys:
        Enter=`\\r`, Tab=`\\t`, Ctrl-C=`\\x03`, Backspace=`\\x7f`,
        Up=`\\x1b[A` Down=`\\x1b[B` Right=`\\x1b[C` Left=`\\x1b[D`.
        So answering a prompt "foo" then Enter = data="foo\\r".

        `wait` controls how long to settle before snapshotting:
        - True (default): wait for the program to react and go QUIET, then return —
          type-then-look in one call. Best for apps that settle after a keystroke.
        - False: fire and return `[sent]` immediately (e.g. mid-stream input).
        - a number (seconds): settle for that FIXED duration, then return the screen.
          Use this for a continuously-animating full-screen app (a spinner / live
          TUI) that NEVER goes quiet — there `wait=True` blocks the whole settle cap
          every keystroke. e.g. `wait=0.4` lets the reaction render, then snapshots.
          The background drainer keeps pyte current during the wait.
        """
        session = self._require()
        # Mark the type-then-look boundary just before the keystroke so wait_idle
        # settles on the REACTION, not on a stale pre-send screen the drainer last
        # touched seconds ago.
        since = time.monotonic()
        session.send(_decode_keys(data))
        # `is False` / `is True`: bool is an int subclass, so a numeric `wait` must
        # be distinguished by identity, not truthiness (0.0 is a valid fixed settle).
        if wait is False:
            return "[sent]"
        if wait is True:
            session.wait_idle(since=since)  # inherit the library settle policy
        else:
            time.sleep(max(0.0, float(wait)))  # fixed settle; drainer updates pyte
        return self._render(session)

    def send_signal(self, sig: int = signal.SIGINT) -> str:
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
        session = self._require()
        # Same type-then-look boundary as send(): settle on the signal's reaction
        # (handler output, exit), not on a stale pre-signal screen.
        since = time.monotonic()
        session.send_signal(sig)
        session.wait_idle(since=since)  # inherit the library settle policy
        return self._render(session)

    def wait(
        self, idle: float = DEFAULT_SETTLE_IDLE, hard: float = DEFAULT_SETTLE_HARD,
    ) -> str:
        """Wait for the program to settle, then return the screen.

        For watching a program that's still working (spinners, streaming) without
        sending input: blocks until it goes quiet for `idle`, or `hard` elapses.
        Counts output already on screen, so if the program is ALREADY quiet this
        returns the current screen promptly rather than blocking for a next burst.
        `idle` = quiet window that counts as settled; `hard` = max wall-clock before
        giving up.
        """
        settled = self._require().wait_idle(idle=idle, hard=hard)
        header = "[settled]" if settled else "[still busy after hard cap]"
        return f"{header}\n{self._render(self._require())}"

    def snapshot(
        self, path: str | None = None, full: bool = False, light: bool = False,
    ) -> str:
        """Render the screen to a PNG (true color, bold weight, box-drawing glyphs)
        and return its path — for SEEING the TUI, not just the grid text view()
        returns. The image reflects the live session's size.

        Renders the current frame by default; full=True (scroll shape) also paints
        scrolled-off history above it — a tall image, so it is opt-in. `path`
        defaults to a fresh temp file. Faint (dim) text isn't shown — pyte discards
        SGR 2 at parse time — but color, bold, and layout are faithful.

        The canvas is a dark terminal by default. Set light=True to render on a
        near-white surface instead — use it to preview an app's *light* theme, which
        tunes its accents for a pale background and so reads wrong on the dark default.
        """
        import os
        import tempfile

        from impty.render import light_surface, render_screen

        session = self._require()
        if path is None:
            fd, path = tempfile.mkstemp(prefix="impty-", suffix=".png")
            os.close(fd)
        bg, fg = light_surface() if light else (None, None)
        with session.paused():  # freeze pyte while render reads the raw screen buffer
            render_screen(session.screen, path, full=full, bg=bg, fg=fg)
        return f"[wrote {path}]"

    def stop(self) -> str:
        """Kill the current program and end the session."""
        if self._session is None:
            return "[no session]"
        self._session.close()
        self._session = None
        return "[stopped]"

    def close(self) -> None:
        """Teardown entry point for the runner: end the session if one is live.
        Idempotent. Distinct from the `stop` tool — `close` returns nothing and is
        called from provider teardown, not by the agent."""
        if self._session is not None:
            self._session.close()
            self._session = None

    def tool_methods(self) -> list[Callable[..., str]]:
        """The bound tool methods, in a fixed order, for the host adapters to
        register. The single enumeration point shared by all three hosts."""
        return [
            self.start, self.view, self.send, self.send_signal,
            self.wait, self.snapshot, self.stop,
        ]
