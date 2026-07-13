# tests/test_prompt_vi_dot.py
from __future__ import annotations

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from imp.cli.prompt import _build_session

ESC = "\x1b"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _drive(
    sends: list[str],
    *,
    editing_mode: str = "vi",
    seed_history: list[str] | None = None,
) -> str:
    """Build a session in the given editing mode and feed it keystrokes.

    seed_history seeds the in-memory history (as imp does across resumes), so
    `k`/`j` and history search reach a prior entry."""
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        session = _build_session(editing_mode=editing_mode)
        for line in seed_history or []:
            session.history.append_string(line)
        for chunk in sends:
            pipe.send_text(chunk)
        result = await session.prompt_async("> ")
    return str(result)


@pytest.mark.anyio
async def test_dot_repeats_delete_char() -> None:
    # "hello" -> ESC (cursor on 'o') -> x deletes 'o' -> "hell" -> . deletes 'l'
    assert await _drive(["hello", ESC, "x", ".", "\r"]) == "hel"


@pytest.mark.anyio
async def test_dot_repeats_delete_line() -> None:
    # Build three lines with `o`; gg to the top; dd deletes L1; . deletes L2.
    seq = ["L1", ESC, "oL2", ESC, "oL3", ESC, "gg", "dd", ".", "\r"]
    assert await _drive(seq) == "L3"


@pytest.mark.anyio
async def test_dot_repeats_delete_word() -> None:
    # 0 -> start; dw deletes "one "; . deletes "two "; "three" remains.
    assert await _drive(["one two three", ESC, "0", "dw", ".", "\r"]) == "three"


@pytest.mark.anyio
async def test_dot_repeats_change_word_at_new_position() -> None:
    # cwZ replaces the first word with Z; w moves; . re-applies the change-word.
    # The exact string reflects prompt_toolkit's own cw/w behavior; the point is
    # that a SECOND word was change-worded (falsifiable: without dot it stays
    # "Z beta gamma"). Trailing "0" separates the replay's ESC from submit.
    seq = ["alpha beta gamma", ESC, "0", "cwZ", ESC, "w", ".", "0", "\r"]
    assert await _drive(seq) == "Zbeta Z"


@pytest.mark.anyio
async def test_dot_repeats_open_line() -> None:
    # o opens a line and inserts X; ESC; . opens+inserts another line.
    # Trailing "0" separates the replay's ESC from submit.
    assert await _drive(["base", ESC, "oX", ESC, ".", "0", "\r"]) == "base\nX\nX"


@pytest.mark.anyio
async def test_dot_repeats_replace_char() -> None:
    # 0 -> start; rX replaces 'a' with 'X' (cursor stays on it); l advances to 'b';
    # . replays rX, replacing 'b' with 'X' -> "XXcd" (falsifiable: no-dot stays "Xbcd").
    assert await _drive(["abcd", ESC, "0", "rX", "l", ".", "\r"]) == "XXcd"


@pytest.mark.anyio
async def test_dot_repeats_counted_delete() -> None:
    # 0 -> start; 3x deletes "abc"; . re-applies the recorded change (3x again).
    assert await _drive(["abcdefgh", ESC, "0", "3x", ".", "\r"]) == "gh"


@pytest.mark.anyio
async def test_dot_repeats_the_change_not_the_undo() -> None:
    # x deletes 'o' -> "hell"; u undoes -> "hello"; . repeats the DELETE -> "hell".
    assert await _drive(["hello", ESC, "x", "u", ".", "\r"]) == "hell"


@pytest.mark.anyio
async def test_dot_with_no_prior_change_is_noop() -> None:
    # . before any change must not raise and must not corrupt the buffer.
    assert await _drive(["hello", ESC, ".", "\r"]) == "hello"


@pytest.mark.anyio
async def test_repeated_dot_repeats_each_time() -> None:
    # 0 -> start; x deletes 'a'; . deletes 'b'; . deletes 'c' -> "def".
    assert await _drive(["abcdef", ESC, "0", "x", ".", ".", "\r"]) == "def"


@pytest.mark.anyio
async def test_literal_dot_in_insert_mode_is_not_intercepted() -> None:
    # A '.' typed while in insert mode inserts normally (no ESC to navigation).
    assert await _drive(["ab.cd", "\r"]) == "ab.cd"


@pytest.mark.anyio
async def test_dot_leaves_macro_register_intact() -> None:
    # Record macro 'a' = "x" (qa x q), then press '.'. Register a, the recording
    # register, and current_recording must be untouched.
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        session = _build_session(editing_mode="vi")
        for chunk in ["hello", ESC, "qa", "x", "q", ".", "\r"]:
            pipe.send_text(chunk)
        result = str(await session.prompt_async("> "))
        vi_state = session.app.vi_state
        register = vi_state.named_registers.get("a")
    assert result == "hel"
    assert register is not None and register.text == "x"
    assert vi_state.recording_register is None
    assert vi_state.current_recording == ""


def test_recorder_installed_on_vi_session() -> None:
    # Smoke test / tripwire: the recorder must be installed on the vi session's
    # key processor. Guards the private _call_handler seam across ptk upgrades.
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        session = _build_session(editing_mode="vi")
        marker = getattr(
            session.app.key_processor._call_handler, "_imp_dot_recorder", False)
    assert marker is True


def test_emacs_session_has_no_dot_recorder() -> None:
    # Emacs mode is completely untouched: no recorder installed.
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        session = _build_session(editing_mode="emacs")
        marker = getattr(
            session.app.key_processor._call_handler, "_imp_dot_recorder", False)
    assert marker is False


@pytest.mark.anyio
async def test_dot_state_does_not_leak_across_prompts() -> None:
    # imp reuses ONE session across prompts. A prompt submitted straight from
    # INSERT mode (Enter without ESC, via `o`) leaves an in-progress insert-change;
    # it must not bleed into the NEXT prompt. Here the second prompt's `.` has no
    # completed change of its own, so it must be a clean no-op — not a replay of
    # the first prompt's `oY`. The trailing "0" separates any replay ESC from submit.
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        session = _build_session(editing_mode="vi")
        for chunk in ["hi", ESC, "oY", "\r"]:  # submits while still in INSERT mode
            pipe.send_text(chunk)
        first = str(await session.prompt_async("> "))
        for chunk in ["abc", ESC, ".", "0", "\r"]:
            pipe.send_text(chunk)
        second = str(await session.prompt_async("> "))
    assert first == "hi\nY"
    assert second == "abc"  # would be "abc\nY" if the stale insert-change leaked


def test_non_repeatable_is_only_undo() -> None:
    # '.' must never repeat an undo, so 'u' stays in the set. c-r in vi
    # navigation mode is reverse-incremental-search, NOT redo (redo has no vi
    # binding in 3.0.52), so it must NOT be specially excluded (defect 3).
    from imp.cli import vi_dot

    assert {"u"} == vi_dot._NON_REPEATABLE


def _recorded_change_keys(session: object) -> list[str]:
    """The keys of the recorder's last recorded change, read from the installed
    _call_handler wrapper's closure (the `state` free variable holds DotState)."""
    wrapped = session.app.key_processor._call_handler  # type: ignore[attr-defined]
    idx = wrapped.__code__.co_freevars.index("state")
    state = wrapped.__closure__[idx].cell_contents
    return [k.key for k in state.last_change] if state.last_change else []


@pytest.mark.anyio
async def test_count_before_undo_does_not_leak_into_next_change() -> None:
    # A count typed before `u` (e.g. `3u`) must be discarded, not folded into the
    # next change: the recorded change for a following `x` must be exactly `["x"]`,
    # never `["3", "x"]` (which would make `.` wrongly replay `3x`).
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        session = _build_session(editing_mode="vi")
        for chunk in ["abcdefgh", ESC, "0", "3", "u", "x", "\r"]:
            pipe.send_text(chunk)
        await session.prompt_async("> ")
        recorded = _recorded_change_keys(session)
    assert recorded == ["x"]


@pytest.mark.anyio
async def test_history_navigation_is_not_recorded_as_change() -> None:
    # With history seeded, x records the delete (abc -> bc). Glancing at the
    # previous entry with k and returning with j moves working_index but edits
    # nothing, so it must NOT overwrite the recorded change: . replays x again
    # (bc -> c). Before the fix, k/j were recorded and . replayed the jump,
    # leaving the buffer "bc".
    result = await _drive(
        ["abc", ESC, "0", "x", "k", "j", ".", "\r"],
        seed_history=["previous entry"],
    )
    assert result == "c"


@pytest.mark.anyio
async def test_search_across_history_is_not_recorded_as_change() -> None:
    # `n` (search-next in navigation mode) applies the current search across
    # history, moving working_index without editing. It must NOT overwrite the
    # recorded change: after x then n, the recorded change stays ["x"]. Before
    # the fix it became ["n"].
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        session = _build_session(editing_mode="vi")
        for line in ["zz1", "zz2"]:
            session.history.append_string(line)
        session.app.current_search_state.text = "zz"
        for chunk in ["abc", ESC, "0", "x", "n", "\r"]:
            pipe.send_text(chunk)
        await session.prompt_async("> ")
        recorded = _recorded_change_keys(session)
    assert recorded == ["x"]
