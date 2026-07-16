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
    """Build a session in the given editing mode and feed it keystrokes."""
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


async def _drive_capture(
    sends: list[str],
    *,
    seed_history: list[str] | None = None,
) -> tuple[str, int]:
    """Drive vi-mode keystrokes, capturing cursor_position just before submit.

    A sentinel `Q` binding snapshots the landed cursor so a test can assert the
    display ROW reached (not merely the final text), pinning j/k to the
    display-row path rather than the logical `cursor_up`/`cursor_down` fallback.
    Send `Q` immediately before the final `\\r`."""
    captured: dict[str, int] = {}
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        session = _build_session(editing_mode="vi")
        for line in seed_history or []:
            session.history.append_string(line)

        @session.key_bindings.add("Q")  # type: ignore[untyped-decorator]
        def _capture(event: object) -> None:
            captured["cursor"] = event.current_buffer.cursor_position  # type: ignore[attr-defined]

        for chunk in sends:
            pipe.send_text(chunk)
        result = await session.prompt_async("> ")
    return str(result), captured["cursor"]


def test_motion_bindings_installed_on_vi_not_emacs() -> None:
    # Tripwire: j binding exists in vi session key bindings, absent from emacs.
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        vi_session = _build_session(editing_mode="vi")
        vi_has_j = any("j" in b.keys for b in vi_session.key_bindings.bindings)

    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        emacs_session = _build_session(editing_mode="emacs")
        emacs_has_j = any("j" in b.keys for b in emacs_session.key_bindings.bindings)

    assert vi_has_j
    assert not emacs_has_j


@pytest.mark.anyio
async def test_k_moves_up_buffer_intact() -> None:
    # Build a 2-line buffer: L1\nL2; press k to move up; submit.
    # History must not be recalled — result is the original buffer text.
    result = await _drive(
        ["L1", ESC, "oL2", ESC, "k", "0", "\r"],
        seed_history=["old history"],
    )
    assert result == "L1\nL2"


@pytest.mark.anyio
async def test_gk_same_result_as_k() -> None:
    result = await _drive(
        ["L1", ESC, "oL2", ESC, "gk", "0", "\r"],
        seed_history=["old history"],
    )
    assert result == "L1\nL2"


@pytest.mark.anyio
async def test_j_and_k_round_trip_buffer_intact() -> None:
    # Build 3-line buffer; gg to top; j down one; k back up; submit.
    # Buffer stays intact — no history recalled.
    result = await _drive(
        ["L1", ESC, "oL2", ESC, "oL3", ESC, "gg", "j", "k", "0", "\r"],
        seed_history=["old history"],
    )
    assert result == "L1\nL2\nL3"


@pytest.mark.anyio
async def test_gj_and_gk_round_trip_buffer_intact() -> None:
    result = await _drive(
        ["L1", ESC, "oL2", ESC, "oL3", ESC, "gg", "gj", "gk", "0", "\r"],
        seed_history=["old history"],
    )
    assert result == "L1\nL2\nL3"


@pytest.mark.anyio
async def test_k_at_top_row_noop_no_history() -> None:
    # Single-line buffer: k at the top must be a no-op, not recall history.
    result = await _drive(
        ["hello", ESC, "k", "\r"],
        seed_history=["old history"],
    )
    assert result == "hello"


@pytest.mark.anyio
async def test_j_at_bottom_row_noop_no_history() -> None:
    # Single-line buffer: j at the bottom must be a no-op, not recall history.
    result = await _drive(
        ["hello", ESC, "j", "\r"],
        seed_history=["old history"],
    )
    assert result == "hello"


@pytest.mark.anyio
async def test_dj_deletes_logical_lines_operator_path_intact() -> None:
    # dj = delete current + next logical line. Our j binding (vi_navigation_mode)
    # must not intercept d+j; prompt_toolkit's default motion handles the operator.
    result = await _drive(
        ["L1", ESC, "oL2", ESC, "oL3", ESC, "gg", "dj", "\r"],
    )
    assert result == "L3"


@pytest.mark.anyio
async def test_dk_deletes_logical_lines_operator_path_intact() -> None:
    # dk from L3: delete L2+L3; prompt_toolkit leaves the preceding \n, so "L1\n".
    result = await _drive(
        ["L1", ESC, "oL2", ESC, "oL3", ESC, "dk", "\r"],
    )
    assert result == "L1\n"


@pytest.mark.anyio
async def test_arrow_up_recalls_history_in_vi_mode() -> None:
    # Arrow up (\x1b[A) must still recall history — arrows are the history escape hatch.
    result = await _drive(
        ["\x1b[A", "\r"],
        seed_history=["prior line"],
    )
    assert result == "prior line"


@pytest.mark.anyio
async def test_gg_lands_at_start_G_at_last_line() -> None:
    # G moves to last logical line; gg moves to first. Both are unaffected by our bindings.
    result = await _drive(
        ["L1", ESC, "oL2", ESC, "oL3", ESC, "gg", "0", "\r"],
    )
    assert result == "L1\nL2\nL3"


@pytest.mark.anyio
async def test_G_lands_at_last_line() -> None:
    # G moves to last logical line without corrupting the buffer or recalling history.
    result = await _drive(
        ["L1", ESC, "oL2", ESC, "oL3", ESC, "gg", "G", "0", "\r"],
        seed_history=["old history"],
    )
    assert result == "L1\nL2\nL3"


@pytest.mark.anyio
async def test_emacs_mode_normal_text_input() -> None:
    # Emacs mode is entirely unaffected — j types 'j', motion bindings absent.
    result = await _drive(
        ["hello", "\r"],
        editing_mode="emacs",
    )
    assert result == "hello"


@pytest.mark.anyio
async def test_emacs_j_inserts_letter() -> None:
    # In emacs mode j is just a letter, confirming motion bindings are not installed.
    result = await _drive(
        ["j", "\r"],
        editing_mode="emacs",
    )
    assert result == "j"


@pytest.mark.anyio
async def test_k_on_wrapped_line_lands_in_first_display_row() -> None:
    # A single logical line of 100 chars wraps at the 78-col usable width
    # (80-col DummyOutput minus the `> ` prompt) into two display rows: 0-77
    # and 78-99. After ESC the cursor is at 99 (second row). `k` moves UP one
    # DISPLAY row to offset 21 (first row). The logical fallback on this lone
    # logical line would be a no-op, leaving the cursor at 99 — so this pins the
    # display-row path. Buffer stays intact; history is never recalled.
    line = "A" * 100
    result, cursor = await _drive_capture(
        [line, ESC, "k", "Q", "\r"], seed_history=["old history"]
    )
    assert result == line
    assert cursor == 21


@pytest.mark.anyio
async def test_j_on_wrapped_line_lands_in_second_display_row() -> None:
    # Same wrapped single logical line. `gg` lands at offset 0 (first display
    # row); `j` moves DOWN one DISPLAY row to offset 78 (start of the second
    # row). The logical fallback would leave the cursor at 0.
    line = "A" * 100
    result, cursor = await _drive_capture(
        [line, ESC, "gg", "j", "Q", "\r"], seed_history=["old history"]
    )
    assert result == line
    assert cursor == 78
