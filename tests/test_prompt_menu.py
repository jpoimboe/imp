"""The slash menu on a real CPR-answering pty; DummyOutput reserves no height and can't draw it."""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from imp.cli.prompt import DEFAULT_HINT
from impty import TerminalSession
from tests.impty.e2e_support import can_pty, clean_env

pytestmark = pytest.mark.skipif(not can_pty(), reason="pty not available")

_PROGRAM = """\
import asyncio, sys
from imp.cli import palette as pal
from imp.cli.prompt import Prompt, _build_session

async def _main() -> None:
    skills_dir, mode, question, pad = sys.argv[1:5]
    pal.set_theme("dark")
    for i in range(int(pad)):
        print(f"pad{i}")
    prompt = Prompt(color=True, _session_factory=lambda: _build_session(
        skills_dir, editing_mode=mode, color=True,
        roster_models=lambda: ["claude-opus-5", "claude-sonnet-5"]))
    while True:
        line = await prompt.ask_async(question=question, bottom_toolbar="status toolbar")
        print(f"GOT: {line!r}")

asyncio.run(_main())
"""

_SKILLS = {
    "sa-bar": "Bar the sa subsystem, carefully",
    "sa-foo": "Foo the sa subsystem end to end",
    "deploy-a": "Deploy to a",
    "deploy-b": "Deploy to b",
}
_S_MENU = ["skills", "sandbox", "sa-bar", "sa-foo"]
_TOOLBAR = "status toolbar"
_IDLE_PROMPT = f"> {DEFAULT_HINT}"


def _screen(session: TerminalSession) -> list[str]:
    return session.lines(limit=session.rows)


def _wait_screen(
    session: TerminalSession, ready: Callable[[list[str]], bool]
) -> list[str]:
    """The settled screen once `ready` holds for it; fails showing the screen if it never does."""
    def probe() -> bool:
        return ready(_screen(session))

    session.wait_idle(idle=0.2, hard=30.0, predicate=probe)
    assert probe(), "\n".join(_screen(session))
    return _screen(session)


def _wait_text(session: TerminalSession, text: str) -> list[str]:
    return _wait_screen(session, lambda rows: any(text in row for row in rows))


def _wait_row(session: TerminalSession, text: str) -> list[str]:
    return _wait_screen(session, lambda rows: any(row.strip() == text for row in rows))


def _wait_menu_closed(session: TerminalSession, line: str, row: int = 0) -> list[str]:
    """`line`, one blank row, the toolbar, and nothing else below: no candidate is left."""
    def closed(rows: list[str]) -> bool:
        below = [line, "", _TOOLBAR]
        return rows[row:] == below + [""] * (len(rows) - row - len(below))

    return _wait_screen(session, closed)


def _settle(session: TerminalSession) -> None:
    # For keys that change nothing on screen, so there is no text to wait on.
    session.wait_idle(idle=0.5, hard=6.0)


def _names(rows: list[str]) -> list[str]:
    return [row.split()[0] for row in rows]


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    user = tmp_path / "skills" / "user"
    user.mkdir(parents=True)
    for name, description in _SKILLS.items():
        (user / f"{name}.md").write_text(f"---\ndescription: {description}\n---\nbody\n")
    return tmp_path / "skills"


@contextlib.contextmanager
def _pty(
    skills_dir: Path, *, rows: int = 24, mode: str = "emacs", question: str = "", pad: int = 0
) -> Iterator[TerminalSession]:
    env = clean_env(PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    argv = [sys.executable, "-c", _PROGRAM, str(skills_dir), mode, question, str(pad)]
    with TerminalSession(argv, env=env, cols=80, rows=rows) as session:
        _wait_row(session, _TOOLBAR)
        yield session


def _open_s_menu(session: TerminalSession) -> list[str]:
    session.send("/s\t")
    return _wait_text(session, "sa-foo")


@pytest.mark.parametrize("mode", ["emacs", "vi"])
def test_typing_shows_no_menu(skills_dir: Path, mode: str) -> None:
    with _pty(skills_dir, mode=mode) as session:
        session.send("/s")
        _wait_row(session, "> /s")
        _settle(session)
        _wait_menu_closed(session, "> /s")


@pytest.mark.parametrize("mode", ["emacs", "vi"])
def test_first_tab_lists_candidates_with_none_selected(skills_dir: Path, mode: str) -> None:
    with _pty(skills_dir, mode=mode) as session:
        rows = _open_s_menu(session)
    assert rows[0] == "> /s"
    assert _names(rows[1:5]) == _S_MENU


@pytest.mark.parametrize("mode", ["emacs", "vi"])
def test_first_tab_does_not_depend_on_key_timing(skills_dir: Path, mode: str) -> None:
    with _pty(skills_dir, mode=mode) as session:
        session.send("/")
        _wait_row(session, "> /")
        session.send("s")
        _wait_row(session, "> /s")
        session.send("\t")
        typed = _wait_text(session, "sa-foo")
    with _pty(skills_dir, mode=mode) as session:
        batched = _open_s_menu(session)
    assert typed == batched


def test_further_tabs_cycle_then_wrap_to_the_typed_text(skills_dir: Path) -> None:
    with _pty(skills_dir) as session:
        _open_s_menu(session)
        for line in ["/skills", "/sandbox", "/sa-bar", "/sa-foo", "/s"]:
            session.send("\t")
            _wait_row(session, f"> {line}")


def test_shift_tab_cycles_backwards(skills_dir: Path) -> None:
    with _pty(skills_dir) as session:
        _open_s_menu(session)
        for line in ["/sa-foo", "/sa-bar"]:
            session.send("\x1b[Z")
            _wait_row(session, f"> {line}")


def test_sole_match_fills_with_no_menu(skills_dir: Path) -> None:
    with _pty(skills_dir) as session:
        session.send("/sk\t")
        _wait_menu_closed(session, "> /skills")


def test_fully_typed_name_changes_nothing(skills_dir: Path) -> None:
    with _pty(skills_dir) as session:
        session.send("/skills")
        _wait_row(session, "> /skills")
        session.send("\t")
        _settle(session)
        _wait_menu_closed(session, "> /skills")


def test_shared_prefix_extends_and_lists_on_the_same_tab(skills_dir: Path) -> None:
    with _pty(skills_dir) as session:
        session.send("/d\t")
        rows = _wait_text(session, "deploy-b")
    assert rows[0] == "> /deploy-"
    assert _names(rows[1:3]) == ["deploy-a", "deploy-b"]


def test_toolbar_moves_down_by_the_menu_height(skills_dir: Path) -> None:
    with _pty(skills_dir) as session:
        assert _screen(session)[1:3] == ["", _TOOLBAR]
        rows = _open_s_menu(session)
    assert rows[5:7] == ["", _TOOLBAR]


def test_long_list_moves_toolbar_by_the_capped_menu_height(skills_dir: Path) -> None:
    for n in range(25):
        (skills_dir / "user" / f"zz{n:02}.md").write_text("---\ndescription: z\n---\nbody\n")
    with _pty(skills_dir, rows=30) as session:
        session.send("/zz\t")
        rows = _wait_text(session, "zz15")
    assert rows[0] == "> /zz"
    assert rows[17:19] == ["", _TOOLBAR]


@pytest.mark.parametrize(
    ("key", "line"),
    [("x", "> /sx"), ("\x7f", "> /"), ("\x1b[D", "> /s"), ("\x07", "> /s")],
    ids=["typing", "backspace", "left", "ctrl-g"],
)
def test_editing_closes_the_menu_and_restores_the_toolbar(
    skills_dir: Path, key: str, line: str
) -> None:
    with _pty(skills_dir) as session:
        _open_s_menu(session)
        session.send(key)
        _wait_menu_closed(session, line)


def test_next_prompt_after_enter_has_no_menu(skills_dir: Path) -> None:
    with _pty(skills_dir) as session:
        _open_s_menu(session)
        session.send("\r")
        rows = _wait_menu_closed(session, _IDLE_PROMPT, row=1)
    assert rows[0] == "GOT: '/s'"


def test_ctrl_c_closes_the_menu_and_clears_the_line(skills_dir: Path) -> None:
    with _pty(skills_dir) as session:
        _open_s_menu(session)
        session.send("\x03")
        _wait_menu_closed(session, _IDLE_PROMPT)


def test_menu_at_terminal_bottom_keeps_scrollback(skills_dir: Path) -> None:
    pads = [f"pad{i}" for i in range(40)]
    with _pty(skills_dir, pad=40) as session:
        rows = _open_s_menu(session)
        assert _names([row for row in rows if not row.startswith(("pad", ">"))][:4]) == _S_MENU
        assert [row for row in session.lines() if row.startswith("pad")] == pads
        session.send("\x07")
        _wait_screen(session, lambda rows: not any("sa-foo" in row for row in rows))
        history = session.lines()
    assert [row for row in history if row.startswith("pad")] == pads
    assert not any(name in row for row in history for name in _S_MENU)


@pytest.mark.parametrize("height", [10, 6, 4])
def test_short_terminal_keeps_prompt_and_toolbar(skills_dir: Path, height: int) -> None:
    with _pty(skills_dir, rows=height) as session:
        session.send("/\t")
        rows = _wait_row(session, "help")
    assert "> /" in rows
    assert _TOOLBAR in rows
    assert not any("Window too small" in row for row in rows)


def test_model_argument_lists_roster_models(skills_dir: Path) -> None:
    with _pty(skills_dir) as session:
        session.send("/model \t")
        rows = _wait_text(session, "claude-sonnet-5")
    assert rows[0] == "> /model claude-"
    assert _names(rows[1:3]) == ["claude-opus-5", "claude-sonnet-5"]


def test_model_argument_fills_the_sole_match(skills_dir: Path) -> None:
    with _pty(skills_dir) as session:
        session.send("/model claude-s\t")
        _wait_menu_closed(session, "> /model claude-sonnet-5")


def test_plain_text_tab_changes_nothing(skills_dir: Path) -> None:
    with _pty(skills_dir) as session:
        session.send("hello")
        before = _wait_row(session, "> hello")
        session.send("\t")
        _settle(session)
        assert _screen(session) == before
        session.send("\r")
        rows = _wait_text(session, "GOT:")
    assert rows[0] == "GOT: 'hello'"


def test_menu_floats_over_lines_below_the_caret(skills_dir: Path) -> None:
    with _pty(skills_dir, rows=12) as session:
        session.send("/a" + "".join(f"\x1b\rline{n}" for n in range(2, 10)))
        _wait_row(session, "line9")
        session.send("\x1b[A" * 8 + "\x05")
        _wait_screen(session, lambda _rows: session.cursor() == (4, 0))
        session.send("\t")
        rows = _wait_text(session, "abandon")
        assert rows[0] == "> /a"
        assert [row.split()[-1] for row in rows[1:3]] == ["archive", "abandon"]
        assert rows[3:11] == [f"  line{n}" for n in range(4, 10)] + ["", _TOOLBAR]

        session.send("\x07")
        restored = _wait_row(session, "line2")
    assert restored[:11] == ["> /a", *[f"  line{n}" for n in range(2, 10)], "", _TOOLBAR]


def test_menu_stays_below_the_caret_under_a_tall_question(skills_dir: Path) -> None:
    question = [f"q{n}" for n in range(1, 9)]
    with _pty(skills_dir, rows=12, question="\n".join(question)) as session:
        session.send("/\t")
        rows = _wait_row(session, "help")
        assert rows[:3] == ["q1", "q2", "> /"]
        assert rows[3].strip() == "help"
        session.send("\x07")
        restored = _wait_row(session, "q8")
    assert restored[:10] == [*question, "", "> /"]
