from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest

from impty.session import TerminalSession
from scripts import capture_imp_ui, snap_imp_ui
from tests.impty.e2e_support import E2E_EXIT_POLL_TIMEOUT, can_pty

pytestmark = pytest.mark.skipif(not can_pty(), reason="pty not available")

_SCENE = Path(snap_imp_ui.__file__).resolve().parent / "capture_imp_ui.py"
_HOURS_ELAPSED = re.compile(r"\b\d+h \d+m\b")

_POSITION_ROW = (
    "seru99 · JSON output for imp list · implementation phase · "
    "plan 1/1 · task 3/4 · claude-opus-5-5 · ██░░░░░░░░ 45k/200k")
_SUBAGENT_ROW = "↳ coder · 24s · claude-sonnet-5-5 · █░░░░░░░░░ 28k/200k"


def test_hero_capture_is_byte_identical_across_runs(tmp_path: Path) -> None:
    first, second = tmp_path / "first.png", tmp_path / "second.png"

    for out in (first, second):
        snap_imp_ui.snap_stream_live(
            out, limit=capture_imp_ui.HERO_LIMIT, cols=capture_imp_ui.HERO_WIDTH,
            rows=snap_imp_ui.HERO_ROWS)

    assert first.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert first.read_bytes() == second.read_bytes()


def test_stream_live_scene_exits_cleanly_leaving_the_footer_on_screen() -> None:
    argv = [sys.executable, str(_SCENE), "stream-live",
            "--limit", str(capture_imp_ui.HERO_LIMIT),
            "--width", str(capture_imp_ui.HERO_WIDTH)]

    with TerminalSession(argv, cols=capture_imp_ui.HERO_WIDTH,
                         rows=snap_imp_ui.HERO_ROWS) as session:
        code = session.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

    rows = session.lines(limit=snap_imp_ui.HERO_ROWS)
    assert code == 0, "\n".join(rows)
    assert rows[-2].rstrip() == _POSITION_ROW
    assert rows[-1].rstrip() == _SUBAGENT_ROW
    # seru99 summed 34m of step work; an hours-long elapsed anywhere on screen
    # means the journal was measured against today instead of the pinned instant.
    assert not [row for row in rows if _HOURS_ELAPSED.search(row)], "\n".join(rows)


def test_stream_live_project_leads_the_status_row() -> None:
    argv = [sys.executable, str(_SCENE), "stream-live",
            "--limit", str(capture_imp_ui.HERO_LIMIT),
            "--width", str(capture_imp_ui.HERO_WIDTH),
            "--project", "imp"]

    with TerminalSession(argv, cols=capture_imp_ui.HERO_WIDTH,
                         rows=snap_imp_ui.HERO_ROWS) as session:
        code = session.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)

    rows = session.lines(limit=snap_imp_ui.HERO_ROWS)
    assert code == 0, "\n".join(rows)
    assert rows[-2].startswith("imp · seru99 · "), "\n".join(rows)


def test_snap_stream_live_passes_project_through_to_the_replay(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sessions: list[TerminalSession] = []

    class _Recording(TerminalSession):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            sessions.append(self)

    monkeypatch.setattr("impty.session.TerminalSession", _Recording)

    snap_imp_ui.main(["stream-live", "--project", "imp",
                      "--out", str(tmp_path / "project.png")])

    rows = sessions[0].lines(limit=snap_imp_ui.HERO_ROWS)
    assert rows[-2].startswith("imp · seru99 · "), "\n".join(rows)
