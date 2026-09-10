from __future__ import annotations

import shutil

import pytest

from impty import TerminalSession
from impty.screen import Regime
from tests.impty.e2e_support import E2E_POLL_TIMEOUT, _poll, can_pty

pytestmark = [
    pytest.mark.skipif(not can_pty(), reason="pty not available"),
    pytest.mark.skipif(shutil.which("vim") is None, reason="vim not installed"),
]

_BANNER = "VIM - Vi IMproved"   # the one banner line vim does not randomize


def test_vim_startup_drops_no_output_over_a_real_pty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # vim's startup burst carries ESC[?4m; before the screen guard, the whole
    # read chunk holding it was dropped and logged.
    vim = shutil.which("vim")
    assert vim is not None
    with (
        caplog.at_level("ERROR", logger="impty.session"),
        TerminalSession([vim, "-u", "NONE"], cols=80, rows=24) as s,
    ):
        assert _poll(lambda: any(_BANNER in r for r in s.lines(limit=s.rows)),
                     timeout=E2E_POLL_TIMEOUT), "vim banner never painted"
        regime = s.regime()
        s.send("\x1b:q!\r")
        assert s.run_to_completion(hard=E2E_POLL_TIMEOUT) == 0
    assert regime is Regime.SCREEN   # vim really was on the alt screen
    assert "impty: unparseable output dropped" not in caplog.text
