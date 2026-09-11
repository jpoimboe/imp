# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Transport-neutral driver for one interactive program over an impty
`TerminalSession`. Owns the session and exposes the eight tool bodies (start,
view, send, send_signal, resize, wait, snapshot, stop) plus helpers. The method
docstrings are the FULL tool descriptions, surfaced only by the standalone stdio
server; both provider hosts (claude_code SDK tools, codex FastMCP) show the
one-line `THIN_DESCRIPTIONS` in `impty.mcp` instead, so agent-facing wording has
to be kept in step across the two. Calls are blocking (wait_idle, time.sleep, pty
reads); each in-process host offloads them to a thread, and this layer clamps
`wait` and a numeric `send`/`resize` settle to `MAX_CALL_HARD`.
"""

from __future__ import annotations

import codecs
import difflib
import os
import pty
import re
import signal
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from impty import TerminalSession
from impty.journal import Journal
from impty.session import (
    DEFAULT_CHANGE_HARD,
    DEFAULT_SETTLE_HARD,
    DEFAULT_SETTLE_IDLE,
    MAX_CALL_HARD,
)

_JOURNAL_RE = re.compile(r"pty-(\d+)\.")

_FULL_SCREEN = "[full screen]"
_NO_TEXT_CHANGE = "[no text change]"


def _rows_phrase(n: int) -> str:
    return "1 row" if n == 1 else f"{n} rows"


def _range_phrase(j1: int, j2: int) -> str:
    return f"row {j1}" if j2 - j1 == 1 else f"rows {j1}-{j2 - 1}"


def _diff_body(prev: list[str], cur: list[str]) -> str:
    """The rows that changed, each hunk labelled with the rows it emits AND the
    rows it consumed, so `prev + body` always rebuilds `cur`. Sequence-aware, not
    index-wise: once the screen scrolls, an index diff reports 23 of 24 rows
    changed on every step forever — worse than sending the whole screen."""
    parts: list[str] = []
    matcher = difflib.SequenceMatcher(None, prev, cur, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            parts.append(f"[{_rows_phrase(i2 - i1)} deleted at row {j1}]")
            continue
        where = _range_phrase(j1, j2)
        if tag == "insert":
            parts.append(f"[{where} inserted]")
        elif i2 - i1 == j2 - j1:
            parts.append(f"[{where}]")
        else:
            parts.append(f"[{where} replacing {_rows_phrase(i2 - i1)}]")
        parts.extend(cur[j1:j2])
    return "\n".join(parts) if parts else _NO_TEXT_CHANGE


def _last_session_index(directory: Path) -> int:
    """Highest existing pty-N.* index: a resumed step reuses its transcript dir,
    and Journal opens "wb" — numbering must continue, not truncate the pre-restart
    attempt (stderr.log opens "ab" for the same reason)."""
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    return max((int(m.group(1)) for p in entries
                if (m := _JOURNAL_RE.match(p.name))), default=0)


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


# The named-key vocabulary send(keys=[...]) resolves. Bytes are the normal-cursor
# (non-DECCKM) xterm forms, matching the TERM=xterm-256color the session defaults to.
_KEY_TABLE: dict[str, str] = {
    "enter": "\r", "return": "\r", "tab": "\t", "shift-tab": "\x1b[Z",
    "escape": "\x1b", "esc": "\x1b", "space": " ", "backspace": "\x7f",
    "delete": "\x1b[3~", "insert": "\x1b[2~",
    "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
    "home": "\x1b[H", "end": "\x1b[F", "pageup": "\x1b[5~", "pagedown": "\x1b[6~",
    "f1": "\x1bOP", "f2": "\x1bOQ", "f3": "\x1bOR", "f4": "\x1bOS",
    "f5": "\x1b[15~", "f6": "\x1b[17~", "f7": "\x1b[18~", "f8": "\x1b[19~",
    "f9": "\x1b[20~", "f10": "\x1b[21~", "f11": "\x1b[23~", "f12": "\x1b[24~",
}

# ctrl-<char> is computed, not tabulated: the control byte is the character's code
# minus 0x40, which covers a-z and the [ \ ] ^ _ punctuation with one rule.
_CTRL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz[\\]^_")

_KEY_VOCABULARY = (
    "enter, tab, shift-tab, escape, space, backspace, delete, insert, up, down, "
    "left, right, home, end, pageup, pagedown, f1-f12, ctrl-<a-z>, alt-<key>"
)


def _resolve_named(name: str, raw: str) -> str | None:
    """One exact spelling (`name`, normalized; `raw` the same name as the caller wrote
    it) to its bytes, or None when it names nothing: a table row, the computed
    `ctrl-<char>` form, or `alt-<rest>` — which prefixes ESC to the remainder's OWN
    staged resolution, falling back to a lone printable char.

    The alt- remainder is taken from `raw`: name-folding may only fold key NAMES, and
    a literal char passed through it would change byte (`alt-A` → ESC a, `alt-+` → ESC
    -) — silently, which is the failure this whole feature removes. Stages 1-2 are
    index-preserving so `raw[4:]` is that remainder, and stage 3 strips the `alt-`
    prefix itself, so this branch never sees a stage-3 candidate."""
    if (found := _KEY_TABLE.get(name)) is not None:
        return found
    if name.startswith("ctrl-") and name[5:] in _CTRL_CHARS:
        return chr(ord(name[5].upper()) - 0x40)
    if name.startswith("alt-"):
        rest = raw[4:]
        body = _resolve_spelling(rest)
        if body is None and len(rest) == 1 and rest.isprintable():
            body = rest
        return None if body is None else f"\x1b{body}"
    return None


def _resolve_spelling(name: str) -> str | None:
    """Try up to three progressively more aggressive spellings, stopping at the first
    that resolves. Staged rather than one pass because no single rule keeps `ctrl-_`
    addressable (an unconditional `_`→`-` destroys it) while still folding `CTRL_C`
    (an unconditional `_`-strip yields the unresolvable `ctrlc`)."""
    lowered = name.lower().replace("+", "-")
    dashed = lowered.replace("_", "-")
    for candidate in (lowered, dashed, dashed.replace("-", "")):
        if (found := _resolve_named(candidate, name)) is not None:
            return found
    return None


def _resolve_key(name: str) -> str:
    """A `send(keys=[...])` name to the bytes a real terminal sends for that key.

    Raises ValueError naming the key AS THE CALLER WROTE IT — an unknown name has to
    be recognizable to whoever typed it, and the message carries the whole vocabulary
    plus the `data=` escape hatch, since this is the one moment an agent needs both."""
    if (found := _resolve_spelling(name)) is not None:
        return found
    raise ValueError(
        f"unknown key name '{name}' — valid: {_KEY_VOCABULARY};"
        " for anything else pass the raw bytes in data="
    )


def _screen_matcher(
    for_text: str | None, pattern: str | None, *, for_change: bool,
) -> Callable[[str], bool] | None:
    """The screen test a `for_text`/`pattern` wait applies, or None when no matcher
    was asked for. Every conflicting combination raises here, before anything blocks
    or a single byte is sent."""
    if for_text is not None and pattern is not None:
        raise ValueError("for_text and pattern are mutually exclusive — pass one")
    if for_change and (for_text is not None or pattern is not None):
        raise ValueError("for_change cannot be combined with for_text/pattern")
    if for_text is not None:
        needle = for_text  # a local: mypy does not narrow a parameter into a lambda
        return lambda screen: needle in screen
    if pattern is None:
        return None
    try:
        # MULTILINE so ^/$ anchor to screen lines; deliberately NOT DOTALL, so `.`
        # never bridges two rows the terminal wrapped apart.
        compiled = re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        raise ValueError(f"invalid pattern: {exc}") from exc
    return lambda screen: compiled.search(screen) is not None


def _capped_header(kind: str, capped: float, requested: float) -> str:
    """The not-settled header, in the one construction `still busy`, `no change` and
    `not seen` share — so the clamp's prose can never drift between them."""
    if requested > MAX_CALL_HARD:
        return (f"[{kind} after {capped:g}s call cap ({requested:g}s requested)"
                " — call wait() again to keep watching]")
    return f"[{kind} after {capped:g}s hard cap]"


def _format_exit(code: int) -> str:
    """The agent-facing spelling of a raw `Popen` returncode: a plain number for a
    normal exit, the signal's NAME for a signal death — never a bare negative. A
    program that handles SIGINT and exits 130 therefore reads `130`, and only a real
    signal death reads `SIGINT`. The single formatter behind both the `view` header
    and `stop()`'s status line, so the two can never diverge."""
    if code >= 0:
        return str(code)
    try:
        return signal.Signals(-code).name
    except ValueError:
        return f"SIG{-code}"


def _fixed_settle(wait: float) -> tuple[float, str]:
    """Clamped fixed-settle seconds, plus the one line a clamped call prefixes to
    the screen (empty when the request already fit inside the ceiling)."""
    requested = float(wait)
    settle = min(max(0.0, requested), MAX_CALL_HARD)
    if requested <= MAX_CALL_HARD:
        return settle, ""
    return settle, f"[waited {settle:g}s call cap ({requested:g}s requested)]\n"


@dataclass(frozen=True, slots=True)
class PtySpawn:
    """An embedding host's replacement for the spawn `start()` would have made
    itself: the argv to exec, the child environment as-is, and the fds that must
    survive the fork. The cwd is the host's to resolve and rides inside `argv`.
    `openpty`, when given, is the host's own pty allocator — for a host whose pty
    must come from somewhere other than this process's devpts; `None` leaves the
    session on `pty.openpty()`."""

    argv: list[str]
    env: dict[str, str]
    pass_fds: tuple[int, ...] = ()
    openpty: Callable[[], tuple[int, int]] | None = None


SandboxHook = Callable[
    [Sequence[str], str | None, Mapping[str, str] | None], PtySpawn | None
]
CwdResolver = Callable[[str | None], str | None]


class ImptyDriver:
    """One lazy `TerminalSession` at a time. `start` refuses to replace a live one.

    The resolver transforms each requested cwd before the sandbox is consulted.
    `PtySpawn` replaces the spawn wholesale; `None` spawns bare. impty imports
    nothing from its host."""

    def __init__(
        self, sandbox: SandboxHook | None = None, journal_dir: Path | None = None,
        snapshot_dir: Path | None = None, cwd_resolver: CwdResolver | None = None,
    ) -> None:
        self._session: TerminalSession | None = None
        self._shown: list[str] | None = None
        self._sandbox = sandbox
        self._cwd_resolver = cwd_resolver
        self._journal_dir = journal_dir
        self._sessions = 0 if journal_dir is None else _last_session_index(journal_dir)
        self._snapshot_dir = snapshot_dir
        if snapshot_dir is not None:
            try:
                snapshot_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                self._snapshot_dir = None

    def _new_journal(self) -> Journal | None:
        """Per-driver numbering: a step that start/stop/starts writes pty-1.* then
        pty-2.*, so the second session cannot overwrite the first."""
        if self._journal_dir is None:
            return None
        self._sessions += 1
        return Journal(self._journal_dir, self._sessions)

    def _require(self) -> TerminalSession:
        if self._session is None:
            raise RuntimeError("no session — call start(argv=[...]) first")
        return self._session

    def _frame(
        self, session: TerminalSession, *, full: bool = False
    ) -> tuple[str, list[str], str]:
        """One consistent read: the status header, the CURRENT screen rows
        (lines(limit=rows)), and the body — that window, or the whole
        history+screen strip with full=True. The cursor is screen-local and
        bounded, so a small y near the bottom is the cheap 'parked = waiting for
        input' tell. Once the child is gone the header carries its exit status in
        an `exit=` field right after `alive=False`; while it runs there is no
        such field."""
        with session.paused():
            cx, cy = session.cursor()
            # One poll, two fields: alive= and exit= can never disagree.
            code = session.returncode
            exit_field = "" if code is None else f" exit={_format_exit(code)}"
            status = (
                f"[alive={code is None}{exit_field} "
                f"regime={session.regime().value} cursor=({cx},{cy})]"
            )
            rows = session.lines(limit=session.rows)
            body = "\n".join(session.lines() if full else rows)
        return status, rows, body

    def _render(self, session: TerminalSession, *, full: bool = False) -> str:
        """Return the complete screen (or whole strip) without a diff or `[full screen]`
        marker, and resynchronizes the changed-row baseline."""
        status, rows, body = self._frame(session, full=full)
        self._shown = rows
        return f"{status}\n{body}"

    def _render_diff(self, session: TerminalSession) -> str:
        """The text change since the last screen the agent was shown.
        Unchanged text uses its marker; changed rows fall back to a marked full
        screen when their diff is not shorter."""
        status, rows, full_body = self._frame(session)
        if self._shown is None:
            body = f"{_FULL_SCREEN}\n{full_body}"
        elif self._shown == rows:
            body = _NO_TEXT_CHANGE
        else:
            diff = _diff_body(self._shown, rows)
            body = diff if len(diff) < len(full_body) else f"{_FULL_SCREEN}\n{full_body}"
        self._shown = rows
        return f"{status}\n{body}"

    def start(
        self,
        argv: list[str],
        cwd: str | None = None,
        cols: int = 80,
        rows: int = 24,
        env: dict[str, str] | None = None,
    ) -> str:
        """Spawn an interactive program on a fresh pty; wait until it settles, exits,
        or the 3s settle deadline elapses, then return `[full screen]` followed by the
        complete screen accumulated so far. This clears the changed-row baseline.

        Refuses while a program is still running — call stop() first. A session whose
        program has exited is replaced. `argv` is the command + args, e.g.
        ["imp", "new", "my-quest"] — spawned directly, so the program is the
        foreground, controlling-tty session leader (Ctrl-C/ESC reach it). No shell
        wrapper is needed; pass `env` to set variables (e.g. API keys) instead of a
        `bash -lc "export …; exec …"` wrapper, which would otherwise put bash — not
        your program — in the foreground group.

        `env` entries are ADDED to the base environment, overriding same-named ones
        (TERM is still defaulted). With a sandbox hook installed by the embedding
        host the program is spawned inside that sandbox and `cwd` is resolved in ITS
        mount namespace; with no hook it runs bare.

        `cols`/`rows` set the terminal size (a real test parameter — programs reflow
        by width). The startup screen can be incomplete; for a slow startup call
        `wait(hard=...)` then `view()`.
        """
        if self._session is not None:
            if self._session.is_alive():
                raise RuntimeError("session already running — call stop() first")
            self._session.close()
            self._session = None
        resolved_cwd = cwd if self._cwd_resolver is None else self._cwd_resolver(cwd)
        plan = None if self._sandbox is None else self._sandbox(argv, resolved_cwd, env)
        journal = self._new_journal()
        try:
            if plan is None:
                # The merge belongs HERE, never inside TerminalSession: a session that
                # unioned os.environ under whatever it was handed would pour the host
                # environment into a sandboxing host's curated child env.
                session = TerminalSession(
                    argv, cwd=resolved_cwd, cols=cols, rows=rows,
                    env={**os.environ, **(env or {})},
                    journal=journal,
                )
            else:
                # cwd=None is load-bearing: Popen's cwd= resolves in the CALLER's mount
                # namespace, so a plan's cwd rides inside plan.argv instead.
                session = TerminalSession(
                    plan.argv, cwd=None, cols=cols, rows=rows,
                    env=plan.env, pass_fds=plan.pass_fds, journal=journal,
                    openpty=pty.openpty if plan.openpty is None else plan.openpty,
                )
        except BaseException:
            if journal is not None:
                journal.close()
            raise
        try:
            session.wait_idle()  # inherit the library settle policy
        except BaseException:
            # It never reached a usable state (and this path may itself be a
            # cancellation) — no SIGTERM courtesy, no second of waiting.
            session.close(grace=0.0)
            raise
        self._session = session
        # Reused drivers must not diff unrelated sessions.
        self._shown = None
        return self._render_diff(session)

    def view(self, full: bool = False) -> str:
        """Read the complete current screen without a diff or `[full screen]` marker, and
        resynchronizes subsequent changed-row responses.

        By default returns the CURRENT screen (what a human sees right now); the
        status header reports liveness, regime (scroll|screen), and cursor position
        (a cursor parked just after a prompt = waiting for input). Once the program
        has exited the header adds its exit status right after `alive=False` —
        `exit=0`, `exit=2`, or a signal name like `exit=SIGINT` for a program killed
        by one; while it is still running there is no `exit=` field. Set full=True to
        get the whole run so far (scrolled-off history + the current screen) — a
        potentially long dump, so it is opt-in.
        """
        return self._render(self._require(), full=full)

    def send(
        self, data: str = "", keys: list[str] | None = None, wait: bool | float = True,
        for_text: str | None = None, pattern: str | None = None,
    ) -> str:
        """Send text and/or named keys to the program. When waiting, return a changed-row
        diff, `[no text change]`, or `[full screen]` followed by the complete screen.

        `data` is literal text the program receives — NOT a description. Escape
        mnemonics in it are decoded: Enter=`\\r`, Tab=`\\t`, Ctrl-C=`\\x03`,
        Backspace=`\\x7f`, Up=`\\x1b[A` Down=`\\x1b[B` Right=`\\x1b[C` Left=`\\x1b[D`.

        `keys` is a list of KEY NAMES, sent after `data` in list order — the safe way
        to send a non-printing key: a mistyped escape in `data` is silently typed into
        the program as text, while a mistyped key NAME is an error that sends nothing.
        So answering a prompt "foo" then Enter = data="foo", keys=["enter"]. Names:
        enter, tab, shift-tab, escape, space, backspace, delete, insert, up, down,
        left, right, home, end, pageup, pagedown, f1-f12, ctrl-<a-z>, alt-<key>
        (spelling is forgiving — `ctrl-c`, `Ctrl+C` and `CTRL_C` are one key). Pass at
        least one of `data` / `keys`; both reach the program in a single write, so
        nothing can land between your text and the key that commits it. For a key the
        list doesn't name, pass its raw bytes in `data`.

        `wait` controls how long to settle before rendering:
        - True (default): wait for the program to react and go QUIET — type-then-look
          in one call. Best for apps that settle after a keystroke.
        - False: `wait=False` returns `[sent]` without rendering (e.g. mid-stream input).
        - a number (seconds): settle for that FIXED duration. Use this for a
          continuously-animating full-screen app (a spinner / live TUI) that NEVER
          goes quiet — there `wait=True` blocks the whole settle cap every keystroke.
          e.g. `wait=0.4` lets the reaction render. The background drainer keeps pyte
          current during the wait. A value over 240s is clamped to it — no single call
          blocks longer — and the reply's first line then reads `[waited 240s call cap
          (600s requested)]`; send again or call `wait()` to keep watching.

        `for_text=` (a plain substring) or `pattern=` (a regex) instead waits for that
        text to APPEAR on screen — the reaction you are actually waiting for, instead
        of guessing a settle time. Pass one, not both, and leave `wait` at True. It
        settles for 0.3s after the match and gives up after 30s; its header is
        `[found]`, `[not seen after <cap>s hard cap]`, or `[not seen — program exited]`.
        For a longer budget send with `wait=False`, then `wait(for_text=…, hard=120)`.
        """
        matcher = _screen_matcher(for_text, pattern, for_change=False)
        if matcher is not None and wait is not True:
            raise ValueError("for_text/pattern needs wait=True (the default)"
                             " — a numeric or False wait contradicts it")
        if not data and not keys:
            raise ValueError("send needs data= text, keys=[...], or both")
        # Resolve the WHOLE list before the first write: an unknown name must deliver
        # nothing at all, half a key list being a state neither side can reason about.
        payload = _decode_keys(data) + "".join(_resolve_key(k) for k in keys or ())
        session = self._require()
        # Mark the type-then-look boundary just before the keystroke so wait_idle
        # settles on the REACTION, not on a stale pre-send screen the drainer last
        # touched seconds ago.
        since = time.monotonic()
        session.send(payload)
        if matcher is not None:
            capped = min(DEFAULT_CHANGE_HARD, MAX_CALL_HARD)
            header = self._await_match(
                session, matcher, DEFAULT_SETTLE_IDLE, capped, DEFAULT_CHANGE_HARD)
            return f"{header}\n{self._render_diff(session)}"
        # `is False` / `is True`: bool is an int subclass, so a numeric `wait` must
        # be distinguished by identity, not truthiness (0.0 is a valid fixed settle).
        if wait is False:
            return "[sent]"
        note = ""
        if wait is True:
            session.wait_idle(since=since)  # inherit the library settle policy
        else:
            settle, note = _fixed_settle(wait)
            time.sleep(settle)  # fixed settle; drainer updates pyte
        return f"{note}{self._render_diff(session)}"

    def send_signal(self, sig: int = signal.SIGINT) -> str:
        """Deliver a real signal to the terminal's foreground process group, then return a
        changed-row diff, `[no text change]`, or `[full screen]` followed by the complete
        screen. The mode-INDEPENDENT interrupt: unlike `send("\\x03")`
        — which only becomes a SIGINT when the tty's line discipline has ISIG on
        (cooked/cbreak) and is just a raw byte at a prompt_toolkit prompt — this
        bypasses the tty entirely. Use it when you want a deterministic Ctrl-C
        regardless of tty mode, instead of scraping the pid and shelling out to `kill`.

        `sig` is the signal number: SIGINT=2 (Ctrl-C, default), SIGTERM=15. It reaches
        exactly what a real Ctrl-C would — the group the terminal holds in the
        foreground — so in a shell it hits the running job rather than the shell, and a
        grandchild that `setsid()`'d into its own session is not signalled. A no-op
        once the child has exited.
        """
        session = self._require()
        # Same type-then-look boundary as send(): settle on the signal's reaction
        # (handler output, exit), not on a stale pre-signal screen.
        since = time.monotonic()
        session.send_signal(sig)
        session.wait_idle(since=since)  # inherit the library settle policy
        return self._render_diff(session)

    def resize(self, rows: int, cols: int, wait: bool | float = True) -> str:
        """Resize the live terminal. When waiting, return a changed-row diff, `[no text
        change]`, or `[full screen]` followed by the complete screen. The child receives
        a real `SIGWINCH`, so a program that reflows on that signal (a TUI relaying
        out, a CLI re-wrapping at a new width) reacts exactly as it would in a real
        terminal that got resized.

        `rows, cols` is the new size — NOTE this order is the REVERSE of
        `start(cols, rows)`; it matches the wrapped `TerminalSession.resize(rows,
        cols)`. pyte only pads/clips its grid to the new geometry; the *program*
        does the actual reflow. A program that ignores `SIGWINCH` just gets a
        re-framed grid.

        `wait` controls how long to settle before rendering (same tri-state as `send`):
        - True (default): wait for the redraw reaction to go QUIET.
        - False: `wait=False` returns `[resized]` without rendering (e.g. a never-quiet
          animating TUI; render separately).
        - a number (seconds): settle for that FIXED duration. A value over 240s is
          clamped to it and the reply's first line then reads `[waited 240s call cap
          (600s requested)]`.
        """
        session = self._require()
        # Mark the boundary before the resize so wait_idle settles on the REDRAW
        # reaction, not on a stale pre-resize screen.
        since = time.monotonic()
        session.resize(rows, cols)
        # `is False` / `is True`: bool is an int subclass, so a numeric `wait` must
        # be distinguished by identity, not truthiness (0.0 is a valid fixed settle).
        if wait is False:
            return "[resized]"
        note = ""
        if wait is True:
            session.wait_idle(since=since)  # inherit the library settle policy
        else:
            settle, note = _fixed_settle(wait)
            time.sleep(settle)  # fixed settle; drainer updates pyte
        return f"{note}{self._render_diff(session)}"

    def _await_match(
        self, session: TerminalSession, matcher: Callable[[str], bool],
        idle: float, capped: float, requested: float,
    ) -> str:
        """Block until the screen matches and then settles, and name the outcome.
        The match travels back on the latched flag, never through wait_idle's bool
        — settledness is the session's answer, matchedness ours — so an unmatched
        True can only be its EOF/child-exited branch, the quiet settle-return being
        suppressed until the predicate fires. `since=0.0` because the predicate is
        itself this wait's gate against a stale screen."""
        found = False

        def probe() -> bool:
            nonlocal found
            found = found or matcher("\n".join(session.lines(limit=session.rows)))
            return found

        settled = session.wait_idle(idle=idle, hard=capped, since=0.0, predicate=probe)
        if found:
            return "[found]"
        if settled:
            return "[not seen — program exited]"
        return _capped_header("not seen", capped, requested)

    def wait(
        self, idle: float = DEFAULT_SETTLE_IDLE, hard: float | None = None,
        for_change: bool = False, for_text: str | None = None,
        pattern: str | None = None,
    ) -> str:
        """Wait until there is an idle window after qualifying output, the process exits,
        or `hard` elapses — or, with a matcher, until its text appears; then return a
        changed-row diff, `[no text change]`, or `[full screen]` followed by the
        complete screen.

        Output already read qualifies normally, so an already-quiet program returns
        promptly. `for_change=True` requires new output after this call, then its idle
        window. A live process with no output is not settled. This follows a long
        command without polling `view`; an animating TUI qualifies on its next repaint.

        `for_text=` (a plain substring) or `pattern=` (a regex; `^`/`$` anchor to
        screen lines and `.` never crosses one) waits for that text to APPEAR — wait
        for a named prompt instead of burning calls polling `view`. Pass one, not
        both, and not with `for_change`. It matches against the CURRENT SCREEN, so
        text already showing returns at once — pick a string unique to the reaction
        you expect — text that has scrolled off does not count, and a string
        spanning a line the terminal wrapped never matches (match a short fragment).
        After it matches the call still settles for `idle`, so you see the finished
        reaction. Its header is `[found]`, `[not seen after <cap>s hard cap]`, or
        `[not seen — program exited]` when the program ended without printing it.

        `hard` defaults to 3s (30s with `for_change=True` or a matcher), caller values
        are accepted, and values over 240s are capped: no single call blocks longer
        than that. Without a matcher the header is `[settled]`,
        `[still busy after <cap>s hard cap]`, or — with `for_change=True` only —
        `[no change after <cap>s hard cap]`. Whenever the cap fires, whichever
        not-settled header applies (`not seen` included) names the call cap and the
        value you asked for and tells you to call wait() again to keep watching —
        which is how you follow a program for hours without any one call blocking
        for hours.
        """
        matcher = _screen_matcher(for_text, pattern, for_change=for_change)
        session = self._require()
        requested = hard if hard is not None else (
            DEFAULT_SETTLE_HARD if matcher is None and not for_change
            else DEFAULT_CHANGE_HARD)
        capped = min(requested, MAX_CALL_HARD)
        if matcher is not None:
            header = self._await_match(session, matcher, idle, capped, requested)
            return f"{header}\n{self._render_diff(session)}"
        # Stamp the boundary BEFORE waiting: in change mode only output read at or
        # after this instant qualifies, so an already-quiet screen can't settle it.
        since = time.monotonic() if for_change else 0.0
        settled = session.wait_idle(idle=idle, hard=capped, since=since)
        if settled:
            header = "[settled]"
        else:
            # The for_change guard is not redundant with the comparison: a child that
            # has produced NO output has last_output_at None, and normal mode must
            # keep reporting that as [still busy after …].
            kind = "no change" if (
                for_change
                and (session.last_output_at is None or session.last_output_at < since)
            ) else "still busy"
            header = _capped_header(kind, capped, requested)
        return f"{header}\n{self._render_diff(session)}"

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
        import tempfile

        from impty.render import light_surface, render_screen

        session = self._require()
        if path is None:
            fd, path = tempfile.mkstemp(
                prefix="impty-", suffix=".png", dir=self._snapshot_dir)
            os.close(fd)
        bg, fg = light_surface() if light else (None, None)
        with session.paused():  # freeze pyte while render reads the raw screen buffer
            render_screen(session.screen, path, full=full, bg=bg, fg=fg)
        return f"[wrote {path}]"

    def stop(self) -> str:
        """End the session and return the program's exit status plus its final screen.

        SIGTERMs the program's process group, gives it a second to clean up (restore
        the terminal, save state, flush) and then SIGKILLs it, so whatever it prints
        on the way out is captured. The first line is `[stopped: exit=…]` — a number
        for a normal exit, a signal name for a signal death, so
        `[stopped: exit=SIGKILL]` is the tell that it ignored SIGTERM. The rest is the
        last screen it painted; there is no liveness header, the session is gone.
        Call `send_signal(9)` first if you want it killed outright. `[no session]`
        when nothing is running.
        """
        session = self._session
        if session is None:
            return "[no session]"
        code = session.close()
        self._session = None
        body = "\n".join(session.lines(limit=session.rows))
        return f"[stopped: exit={_format_exit(code)}]\n{body}"

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
            self.start, self.view, self.send, self.send_signal, self.resize,
            self.wait, self.snapshot, self.stop,
        ]
