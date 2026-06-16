"""End-to-end ESC test: drives `_async_cmd_resume` against a real pty.openpty()
slave (substituted for /dev/tty) and asserts rc 130 on an ESC keystroke.
In-process; stubs run_change to sleep-until-cancel."""

from __future__ import annotations

import argparse
import contextlib
import os
import threading
import time
from pathlib import Path

import anyio
import pytest

from imp.cli import main as cli_main
from imp.cli.signals import InterruptCoordinator
from imp.engine.executor import RunResult
from tests.conftest import _seed_control


def _can_pty() -> bool:
    try:
        import pty
        m, s = pty.openpty()
        os.close(m)
        os.close(s)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _can_pty(), reason="pty not available")


def test_esc_keystroke_mid_run_exits_with_rc_130(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import pty

    from imp.engine.change import Change
    from imp.engine.state import State

    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="esc", change_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)
    monkeypatch.setattr(cli_main, "_control_root", lambda: tmp_path)

    master, slave = pty.openpty()
    real_open = os.open

    def _fake_open(path: str, flags: int, mode: int = 0o777) -> int:
        if path == "/dev/tty":
            return os.dup(slave)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", _fake_open)

    async def _fake_run_change(_change: object, **kw: object) -> RunResult:
        coord = kw["coordinator"]
        assert isinstance(coord, InterruptCoordinator)
        for _ in range(300):
            if coord.should_pause:
                return RunResult.PAUSED
            await anyio.sleep(0.02)
        raise AssertionError("ESC never observed by coordinator")

    monkeypatch.setattr(cli_main, "run_change", _fake_run_change)

    def _writer() -> None:
        time.sleep(0.5)
        os.write(master, b"\x1b")

    threading.Thread(target=_writer, daemon=True).start()
    try:
        args = argparse.Namespace(token="feat", from_target=None, force=False)
        rc = anyio.run(cli_main._async_cmd_resume, args)
    finally:
        with contextlib.suppress(OSError):
            os.close(master)
        with contextlib.suppress(OSError):
            os.close(slave)

    out = capsys.readouterr().out
    assert rc == 130
    assert "unclassified provider error" not in out
    assert "Cannot write to terminated process" not in out
