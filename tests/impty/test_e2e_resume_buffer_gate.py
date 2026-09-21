"""The buffered resume latch is bounded to ONE gate, and the stop ack dies with it.

Drives the real client stack (LiveStatus + LiveDriver + InterruptCoordinator +
run_terminal_client + make_buffered_solicit(make_interactive_solicit(...))) over a pty,
against an in-process session daemon in the child:

  interrupt gate: the latch is SPENT without delivering — the buffered line is never
                  committed as a `>` reply and the real steer prompt takes the screen.
  bare gate:      the latch delivers with no prompt, and the sticky `stopping…` ack is
                  cleared by the solicit finally, so the composed footer comes back and
                  the ack never returns while output scrolls.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from imp.engine.completion import INTERRUPT_STEER_HINT
from imp.engine.quest import Quest
from impty import TerminalSession
from tests.conftest import _seed_control
from tests.impty.e2e_support import (
    E2E_EXIT_POLL_TIMEOUT,
    E2E_POLL_TIMEOUT,
    E2E_RENDER_POLL_TIMEOUT,
    _poll,
    can_pty,
    clean_env,
)

pytestmark = pytest.mark.skipif(not can_pty(), reason="pty not available")

_HARNESS = str(Path(__file__).parent / "_resume_buffer_harness.py")
_WORKTREE_SRC = str(Path(__file__).parent.parent.parent / "src")

_ACK = "stopping…"
_BUFFERED = "BUFFEREDANSWER"      # the harness's latched first line
_BLOCK = "NARRATIVEBLOCK"         # agent output, one numbered block per event
_STEP = "planner"                 # in the footer from the first step_started
# The composed footer's identity row is `<title> · <model>`; the title appears
# nowhere else on screen, so it is the stable "footer is back" token.
_TITLE = "FOOTERTITLE"


def _screen(s: TerminalSession) -> list[str]:
    return s.lines(limit=s.rows)


def _child_env() -> dict[str, str]:
    # clean_env strips the outer IMP_* (notably IMP_STATE_DIR, which would make the
    # child refuse the scratch root) but keeps IMP_HOME, which is where the seeded
    # quest home lives.
    return clean_env(
        PYTHONPATH=_WORKTREE_SRC
        + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
    )


def _seed_quest(tmp_path: Path) -> Quest:
    _seed_control(tmp_path)
    return Quest.create(tmp_path, title=_TITLE, quest_id="feat", base_branch="main")


def _spawn(tmp_path: Path, ctl: Path, quest: Quest, mode: str) -> TerminalSession:
    return TerminalSession(
        [sys.executable, _HARNESS, str(ctl), str(tmp_path), quest.id, mode],
        cwd=str(tmp_path), env=_child_env(), cols=80, rows=24)


def test_interrupt_gate_spends_the_latch_without_committing_the_buffered_line(
    tmp_path: Path,
) -> None:
    quest = _seed_quest(tmp_path)
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    with _spawn(tmp_path, ctl, quest, "interrupt") as s:
        assert _poll(lambda: any(_STEP in r for r in _screen(s)),
                     timeout=E2E_POLL_TIMEOUT), "footer never painted"
        (ctl / "gesture").touch()
        assert _poll(lambda: any(_ACK in r for r in _screen(s)),
                     timeout=E2E_POLL_TIMEOUT), "ack never painted"

        # The interrupt gate SPENDS the latch: the real prompt opens with the
        # interrupt steer hint as its placeholder. That repaint trails the gate
        # event by a socket hop plus a prompt_toolkit render — the render-bound
        # budget, not the base poll.
        (ctl / "gate").touch()
        assert _poll(lambda: any("type to redirect" in r for r in _screen(s)),
                     timeout=E2E_RENDER_POLL_TIMEOUT), "steer prompt never drew"
        assert any(INTERRUPT_STEER_HINT in r for r in _screen(s)), _screen(s)
        assert not any(_BUFFERED in r for r in s.lines()), (
            "the buffered line was committed at the interrupt gate")

        # The prompt tcflushes typed-ahead bytes when it opens, so only type
        # once it is up (asserted above).
        s.wait_idle()
        s.send("STEERLINE\r")
        assert _poll(lambda: any("STEERLINE" in r for r in _screen(s)),
                     timeout=E2E_POLL_TIMEOUT), "typed steer line never echoed"
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)
    assert rc == 0
    assert not any(_BUFFERED in r for r in s.lines()), (
        "the buffered line reached the scrollback")


def test_bare_gate_delivers_the_latch_and_the_ack_dies_with_the_solicit(
    tmp_path: Path,
) -> None:
    quest = _seed_quest(tmp_path)
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    with _spawn(tmp_path, ctl, quest, "bare") as s:
        assert _poll(lambda: any(_STEP in r for r in _screen(s)),
                     timeout=E2E_POLL_TIMEOUT), "footer never painted"
        # The ack paints BEFORE the gate and is sticky across the spinner.
        (ctl / "gesture").touch()
        assert _poll(lambda: any(_ACK in r for r in _screen(s)),
                     timeout=E2E_POLL_TIMEOUT), "ack never painted"
        assert not any(_TITLE in r for r in _screen(s)), (
            "the ack did not replace the composed footer")

        # The bare gate delivers the latched line with NO prompt (nothing
        # suspends the status), so only the solicit finally can end the ack.
        (ctl / "gate").touch()
        assert _poll(
            lambda: (not any(_ACK in r for r in _screen(s))
                     and any(_TITLE in r for r in _screen(s))),
            timeout=E2E_RENDER_POLL_TIMEOUT), "ack outlived the latched reply"
        assert _poll(lambda: any(f"> {_BUFFERED}" in r for r in _screen(s)),
                     timeout=E2E_POLL_TIMEOUT), "latched reply never committed"

        (ctl / "output").touch()
        assert _poll(lambda: any(f"{_BLOCK} 2" in r for r in _screen(s)),
                     timeout=E2E_POLL_TIMEOUT), "output never resumed"
        blocks: set[str] = set()
        for i in range(8):
            time.sleep(0.2)
            rows = _screen(s)
            assert not any(_ACK in r for r in rows), f"ack came back at sample {i}"
            blocks.update(r.strip() for r in rows if _BLOCK in r)
        assert len(blocks) > 4, f"output did not scroll while sampling: {blocks}"
        assert _poll(lambda: any(_TITLE in r for r in _screen(s)),
                     timeout=E2E_POLL_TIMEOUT), "footer stopped repainting"

        (ctl / "quit").touch()
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)
    assert rc == 0
    assert not any(_ACK in r for r in s.lines()), "ack left residue after teardown"
