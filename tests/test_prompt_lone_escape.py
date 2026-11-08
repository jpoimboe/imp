"""A lone ESC at the input prompt takes effect at once, on a real pty."""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from imp.cli.prompt import DEFAULT_HINT
from impty import TerminalSession
from impty.journal import Journal
from tests.impty.e2e_support import can_pty, clean_env

pytestmark = pytest.mark.skipif(not can_pty(), reason="pty not available")

_PROGRAM = """\
import asyncio, sys
from imp.cli.prompt import Prompt

async def _main() -> None:
    line = await Prompt(editing_mode=sys.argv[1]).ask_async()
    print(f"GOT: {line!r}")

asyncio.run(_main())
"""

ESC = "\x1b"
_IDLE_PROMPT = f"> {DEFAULT_HINT}"
_BLOCK_CURSOR = b"\x1b[2 q"
# Longer than the 50ms parser timeout, far shorter than the old ~1.5s lone-ESC delay.
_ESC_GAP = 0.2
# Under the global 60s pytest timeout, so a hung wait fails with the screen dumped.
_WAIT = 30.0


@dataclasses.dataclass(frozen=True)
class _Prompt:
    session: TerminalSession
    raw: Path

    def screen(self) -> list[str]:
        return self.session.lines(limit=self.session.rows)

    def wait(self, ready: Callable[[], bool]) -> float:
        """Poll until `ready` holds; returns the monotonic time it was first seen."""
        deadline = time.monotonic() + _WAIT
        while not ready():
            assert time.monotonic() < deadline, "\n".join(self.screen())
            time.sleep(0.01)
        return time.monotonic()

    def wait_row(self, row: str) -> None:
        self.wait(lambda: row in self.screen())

    def wait_cursor(self, x: int, y: int) -> None:
        self.wait(lambda: self.session.cursor() == (x, y))

    def type(self, keys: str, row: str) -> None:
        self.session.send(keys)
        self.wait_row(row)

    def lone_esc(self) -> None:
        self.session.send(ESC)
        time.sleep(_ESC_GAP)

    def esc_to_normal(self) -> None:
        """Vi: a lone ESC shows the block cursor within 1s (the old delay was ~1.5s)."""
        start = self.raw.stat().st_size
        sent = time.monotonic()
        self.session.send(ESC)
        shown = self.wait(lambda: _BLOCK_CURSOR in self.raw.read_bytes()[start:])
        assert shown - sent < 1.0

    def submitted(self) -> str:
        def got() -> list[str]:
            return [row for row in self.screen() if row.startswith("GOT: ")]

        self.wait(lambda: bool(got()))
        value = ast.literal_eval(got()[0].removeprefix("GOT: "))
        assert isinstance(value, str)
        return value


@contextlib.contextmanager
def _prompt(tmp_path: Path, mode: str) -> Iterator[_Prompt]:
    env = clean_env(PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    argv = [sys.executable, "-c", _PROGRAM, mode]
    with TerminalSession(argv, env=env, cols=80, rows=24, journal=Journal(tmp_path, 0)) as session:
        prompt = _Prompt(session, tmp_path / "pty-0.raw")
        prompt.wait_row(_IDLE_PROMPT)
        yield prompt


def test_emacs_esc_then_enter_submits(tmp_path: Path) -> None:
    with _prompt(tmp_path, "emacs") as p:
        p.type("hello", "> hello")
        p.lone_esc()
        p.session.send("\r")
        assert p.submitted() == "hello"


def test_vi_esc_then_enter_submits(tmp_path: Path) -> None:
    with _prompt(tmp_path, "vi") as p:
        p.type("hello", "> hello")
        p.esc_to_normal()
        p.session.send("\r")
        assert p.submitted() == "hello"


def test_emacs_esc_does_not_swallow_meta_letter(tmp_path: Path) -> None:
    with _prompt(tmp_path, "emacs") as p:
        p.lone_esc()
        p.session.send("done\r")
        assert p.submitted() == "done"


@pytest.mark.parametrize("mode", ["emacs", "vi"])
def test_alt_enter_in_one_write_inserts_newline(tmp_path: Path, mode: str) -> None:
    with _prompt(tmp_path, mode) as p:
        p.type("ab", "> ab")
        p.session.send("\x1b\r")
        p.wait_cursor(2, 1)
        p.session.send("cd\r")
        assert p.submitted() == "ab\ncd"


def test_emacs_alt_b_in_one_write_moves_back_a_word(tmp_path: Path) -> None:
    with _prompt(tmp_path, "emacs") as p:
        p.type("foo bar", "> foo bar")
        p.session.send("\x1bb")
        p.wait_cursor(6, 0)
        p.session.send("X\r")
        assert p.submitted() == "foo Xbar"


def test_emacs_alt_f_in_one_write_moves_forward_a_word(tmp_path: Path) -> None:
    with _prompt(tmp_path, "emacs") as p:
        p.type("foo bar", "> foo bar")
        p.session.send("\x01")
        p.wait_cursor(2, 0)
        p.session.send("\x1bf")
        p.wait_cursor(5, 0)
        p.session.send("X\r")
        assert p.submitted() == "fooX bar"


def test_vi_esc_enters_normal_mode_at_once(tmp_path: Path) -> None:
    with _prompt(tmp_path, "vi") as p:
        p.type("hello", "> hello")
        p.esc_to_normal()
        p.type("dd", _IDLE_PROMPT)
        p.session.send("\r")
        assert p.submitted() == ""


def test_vi_dot_repeat_then_quick_enter_submits(tmp_path: Path) -> None:
    with _prompt(tmp_path, "vi") as p:
        p.type("ab", "> ab")
        p.esc_to_normal()
        p.type("acd", "> abcd")
        p.esc_to_normal()
        # `.` replays ESC last; Enter must follow inside the old 1s window to catch the bug.
        p.type(".", "> abcdcd")
        p.session.send("\r")
        assert p.submitted() == "abcdcd"


def test_emacs_esc_accepts_search(tmp_path: Path) -> None:
    with _prompt(tmp_path, "emacs") as p:
        p.type("hello", "> hello")
        p.session.send("\x12")
        p.wait(lambda: any("I-search backward:" in row for row in p.screen()))
        p.session.send("ell")
        p.wait(lambda: any("I-search backward: ell" in row for row in p.screen()))
        p.lone_esc()
        p.session.send("\r")
        assert p.submitted() == "hello"


def test_vi_esc_accepts_search(tmp_path: Path) -> None:
    with _prompt(tmp_path, "vi") as p:
        p.type("hello", "> hello")
        p.esc_to_normal()
        p.session.send("/ell")
        # PromptSession's SearchToolbar is not built with vi_mode, so vi `/` shows I-search text.
        p.wait(lambda: any("I-search backward: ell" in row for row in p.screen()))
        p.lone_esc()
        p.session.send("\r")
        assert p.submitted() == "hello"


@pytest.mark.parametrize(("mode", "quote"), [("emacs", "\x11"), ("vi", "\x16")])
def test_quoted_insert_inserts_literal_esc(tmp_path: Path, mode: str, quote: str) -> None:
    with _prompt(tmp_path, mode) as p:
        p.type("ab", "> ab")
        p.session.send(quote)
        time.sleep(_ESC_GAP)
        p.lone_esc()
        p.session.send("x\r")
        assert p.submitted() == "ab\x1bx"
