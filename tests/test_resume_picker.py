from __future__ import annotations

from datetime import UTC, datetime

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from imp.cli.resume_select import Candidate, ResumePicker, run_picker

_NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)
_HEADER = "Resume which change?"


def _cands() -> list[Candidate]:
    return [
        Candidate(quest_id="2026-06-11-aaa111", title="Add version flag",
                  status="paused", step="plan", last_iso="2026-06-11T11:45:00+00:00"),
        Candidate(quest_id="2026-06-10-bbb222", title="Other change",
                  status="new", step="spec", last_iso="2026-06-10T11:45:00+00:00"),
    ]


def test_newest_is_preselected() -> None:
    p = ResumePicker(_cands(), now=_NOW, color=False, header=_HEADER)
    assert p.index == 0
    assert p.selected().quest_id == "2026-06-11-aaa111"


def test_move_clamps_at_both_ends() -> None:
    p = ResumePicker(_cands(), now=_NOW, color=False, header=_HEADER)
    p.move_up()                                  # already top → clamp
    assert p.index == 0
    p.move_down()
    assert p.index == 1
    p.move_down()                                # already bottom → clamp
    assert p.index == 1
    assert p.selected().quest_id == "2026-06-10-bbb222"


def test_render_marks_selection_and_uses_shared_formatter() -> None:
    lines = ResumePicker(_cands(), now=_NOW, color=False, header=_HEADER).render().splitlines()
    assert lines[0].startswith("▸ ")             # selected (newest) gets the marker
    assert lines[1].startswith("  ")             # unselected gets blank gutter
    assert "Add version flag" in lines[0]        # TITLE via change_row formatter
    assert "aaa111" in lines[0]                  # ID = resume token (modern-id suffix)
    assert "15m" in lines[0]                     # relative AGE (11:45 vs 12:00 _NOW)


def test_render_marker_moves_after_navigation() -> None:
    p = ResumePicker(_cands(), now=_NOW, color=False, header=_HEADER)
    p.move_down()
    lines = p.render().splitlines()
    assert lines[0].startswith("  ")             # first row no longer selected
    assert lines[1].startswith("▸ ")             # marker moved to second row


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_picker_enter_resumes_preselected_newest() -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\r")                     # enter at once → newest (index 0)
        chosen = await run_picker(_cands(), now=_NOW, color=False, header=_HEADER)
    assert chosen == "2026-06-11-aaa111"


@pytest.mark.anyio
async def test_picker_down_then_enter_selects_second() -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\x1b[B")                 # down arrow
        pipe.send_text("\r")                     # enter
        chosen = await run_picker(_cands(), now=_NOW, color=False, header=_HEADER)
    assert chosen == "2026-06-10-bbb222"


@pytest.mark.anyio
async def test_picker_q_cancels_to_none() -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("q")
        chosen = await run_picker(_cands(), now=_NOW, color=False, header=_HEADER)
    assert chosen is None


@pytest.mark.anyio
async def test_picker_escape_cancels_to_none() -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\x1b")                   # bare ESC
        chosen = await run_picker(_cands(), now=_NOW, color=False, header=_HEADER)
    assert chosen is None


@pytest.mark.anyio
async def test_picker_ctrl_c_cancels_to_none() -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\x03")                   # ctrl-c (a key here, not an OS signal)
        chosen = await run_picker(_cands(), now=_NOW, color=False, header=_HEADER)
    assert chosen is None


def test_picker_stores_header_and_preselected_index() -> None:
    p = ResumePicker(_cands(), now=_NOW, color=False,
                     header="Abandon which change?", selected_index=1)
    assert p.header == "Abandon which change?"
    assert p.index == 1
    assert p.selected().quest_id == "2026-06-10-bbb222"


@pytest.mark.anyio
async def test_picker_enter_with_preselect_returns_that_index() -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text("\r")                     # enter at once → the pre-selected row
        chosen = await run_picker(_cands(), now=_NOW, color=False,
                                  header="Show log for which change?", selected_index=1)
    assert chosen == "2026-06-10-bbb222"
