from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from impty import TerminalSession
from tests.impty.e2e_support import (
    E2E_EXIT_POLL_TIMEOUT,
    E2E_POLL_TIMEOUT,
    _poll,
    can_pty,
    clean_env,
)

pytestmark = pytest.mark.skipif(not can_pty(), reason="pty not available")

_HARNESS = str(Path(__file__).parent / "_stop_ack_harness.py")
_ACK = "stopping…"


def _screen(s: TerminalSession) -> list[str]:
    return s.lines(limit=s.rows)


def test_stop_ack_survives_the_spinner_its_own_events_and_a_narrative_block(
    tmp_path: Path,
) -> None:
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    with TerminalSession([sys.executable, _HARNESS, str(ctl)],
                         cwd=str(tmp_path), env=clean_env(),
                         cols=80, rows=24) as s:
        assert _poll(lambda: any("planner" in r for r in _screen(s)),
                     timeout=E2E_POLL_TIMEOUT), "footer never painted"
        (ctl / "gesture").touch()
        assert _poll(lambda: any(_ACK in r for r in _screen(s)),
                     timeout=E2E_POLL_TIMEOUT), "ack never painted"

        for i in range(6):
            time.sleep(0.12)
            assert any(_ACK in r for r in _screen(s)), f"ack gone at sample {i}"

        (ctl / "events").touch()
        for i in range(6):
            time.sleep(0.12)
            assert any(_ACK in r for r in _screen(s)), f"ack gone after events, sample {i}"

        (ctl / "block").touch()
        assert _poll(lambda: any("NARRATIVEBLOCK" in r for r in _screen(s)),
                     timeout=E2E_POLL_TIMEOUT), "block never printed"
        for i in range(6):
            time.sleep(0.12)
            rows = _screen(s)
            assert any(_ACK in r for r in rows), f"ack gone after block, sample {i}"
            b = next((idx for idx, r in enumerate(rows) if "NARRATIVEBLOCK" in r), None)
            assert b is not None, rows
            a = next((idx for idx, r in enumerate(rows) if _ACK in r), None)
            assert a is not None, rows
            assert a > b, f"ack not repainted below the block: {rows}"

        (ctl / "prompt").touch()
        assert _poll(lambda: any("PROMPTHERE" in r for r in _screen(s)),
                     timeout=E2E_POLL_TIMEOUT), "prompt never drew"
        # screen is stable: child is in status.suspend() where _suspended makes tick() a no-op
        time.sleep(0.5)
        rows = _screen(s)
        assert not any(_ACK in r for r in rows), f"ack survived the prompt: {rows}"
        p = next((idx for idx, r in enumerate(rows) if "PROMPTHERE" in r), None)
        assert p is not None, rows
        assert p >= 2, rows
        assert rows[p - 1].strip() == ""
        assert rows[p - 2].strip() != ""

        (ctl / "quit").touch()
        rc = s.run_to_completion(hard=E2E_EXIT_POLL_TIMEOUT)
    assert rc == 0
    assert not any(_ACK in r for r in s.lines()), "ack left residue after teardown"
