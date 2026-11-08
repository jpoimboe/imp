from __future__ import annotations

import sys
from pathlib import Path

import anyio
import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from imp.cli.abandon_confirm import abandon_question, ask_keypress_confirm, removal_description
from imp.engine.quest import Quest
from tests.conftest import _commit_work, _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize(("n", "phrase"), [
    (0, "no commits"), (1, "1 commit"), (3, "3 commits"),
])
def test_removal_description_counts_commits_ahead_of_base(
    tmp_path: Path, n: int, phrase: str,
) -> None:
    _seed_control(tmp_path)
    quest = Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    _commit_work(quest, n)
    assert removal_description(quest) == f"worktree and branch imp/feat ({phrase})"


def test_removal_description_is_none_for_a_draft(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    draft = Quest.create_draft(tmp_path, title="An idea", quest_id="idea1",
                               base_branch="main", origin="idea",
                               seed_opening="An idea\n\ndo a thing")
    assert removal_description(draft) is None


def test_abandon_question_matches_pinned_copy() -> None:
    assert abandon_question(
        "feat01", "Add feature", "worktree and branch imp/feat01 (5 commits)",
    ) == ('Abandon "feat01 · Add feature"? This removes its worktree and branch '
          "imp/feat01 (5 commits). [y/N] ")


_Q = abandon_question("feat01", "Add feature", "worktree and branch imp/feat01 (5 commits)")


async def _press(keys: str) -> bool:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(keys)
        with anyio.fail_after(10):
            return await ask_keypress_confirm(_Q)


@pytest.mark.anyio
@pytest.mark.parametrize("key", ["y", "Y"])
async def test_keypress_confirm_y_confirms_without_enter(
    capsys: pytest.CaptureFixture[str], key: str,
) -> None:
    assert await _press(key) is True
    assert capsys.readouterr().out == _Q + "y\n"


@pytest.mark.anyio
@pytest.mark.parametrize(("name", "key"), [
    ("n", "n"), ("enter", "\r"), ("esc", "\x1b"), ("ctrl-c", "\x03"), ("ctrl-d", "\x04"),
])
async def test_keypress_confirm_any_other_key_declines(
    capsys: pytest.CaptureFixture[str], name: str, key: str,
) -> None:
    assert await _press(key) is False
    assert capsys.readouterr().out == _Q + "n\n"


@pytest.mark.anyio
async def test_keypress_confirm_answers_on_the_first_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert await _press("ny") is False
    assert capsys.readouterr().out == _Q + "n\n"


@pytest.mark.anyio
async def test_keypress_confirm_flushes_tty_stdin_before_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import termios

    class _TtyStdin:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 0

    flushed: list[tuple[int, int]] = []
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        def _fake_tcflush(fd: int, queue: int) -> None:
            flushed.append((fd, queue))
            pipe.send_text("y")   # the only key arrives after the flush

        monkeypatch.setattr(sys, "stdin", _TtyStdin())
        monkeypatch.setattr(termios, "tcflush", _fake_tcflush)
        with anyio.fail_after(10):
            assert await ask_keypress_confirm(_Q) is True
    assert flushed == [(0, termios.TCIFLUSH)]
