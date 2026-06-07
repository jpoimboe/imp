"""The post-run PAUSED branch of `_async_cmd_new`/`_async_cmd_resume` must read
`coord.pause_reason` (default `ctrl-c`) so the `paused (...)` headline matches
the originating source."""

from __future__ import annotations

import argparse
from pathlib import Path

import anyio
import pytest

from imp.cli import main as cli_main
from imp.cli.signals import InterruptCoordinator
from imp.engine.change import Change
from imp.engine.executor import RunResult
from tests.conftest import _seed_control


@pytest.mark.parametrize(
    ("pause_reason", "expected_headline"),
    [("ctrl-c", "paused (ctrl-c)"),
     ("esc", "paused (esc)"),
     ("sigterm", "paused (sigterm)"),
     (None, "paused (ctrl-c)")],
)
def test_new_paused_headline_reflects_pause_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pause_reason: str | None,
    expected_headline: str,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    async def _fake_run_change(_change: Change, **kw: object) -> RunResult:
        coord = kw["coordinator"]
        assert isinstance(coord, InterruptCoordinator)
        if pause_reason is not None:
            coord.pause_reason = pause_reason
        coord._should_pause = True
        return RunResult.PAUSED

    reasons: list[str] = []
    monkeypatch.setattr(cli_main, "run_change", _fake_run_change)
    monkeypatch.setattr(
        cli_main, "pause_and_exit",
        lambda _change, *, reason: reasons.append(reason),
    )

    args = argparse.Namespace(title="headline test")
    rc = anyio.run(cli_main._async_cmd_new, args)
    out = capsys.readouterr().out

    assert rc == 130
    assert expected_headline in out
    assert reasons == [pause_reason or "ctrl-c"]
