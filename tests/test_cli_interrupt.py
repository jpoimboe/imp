"""Ctrl-C / Ctrl-D at the interactive prompt must pause, not crash.

prompt_toolkit's prompt_async (handle_sigint=True by default) installs its own
SIGINT handler and raises KeyboardInterrupt while the `> ` prompt is active,
bypassing the InterruptCoordinator. The CLI command frame must catch it and
pause — exactly as it already catches EOFError (Ctrl-D). Regression test for the
uncaught-KeyboardInterrupt crash (BaseExceptionGroup escaping the task group).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import anyio
import pytest

from imp.cli import main as cli_main
from tests.conftest import _seed_control


@pytest.mark.parametrize(
    ("exc", "reason"),
    [(KeyboardInterrupt, "ctrl-c"), (EOFError, "ctrl-d")],
)
def test_new_interrupt_at_prompt_pauses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc: type[BaseException],
    reason: str,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    async def _raise(*_a: object, **_k: object) -> object:
        raise exc

    reasons: list[str] = []
    monkeypatch.setattr(cli_main, "run_change", _raise)
    monkeypatch.setattr(
        cli_main, "pause_and_exit",
        lambda _change, *, reason, engine_pause_reason=None: reasons.append(reason),
    )

    args = argparse.Namespace(title="interrupt at prompt")
    rc = anyio.run(cli_main._async_cmd_new, args)

    assert rc == 130
    assert reasons == [reason]
