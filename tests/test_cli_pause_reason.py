"""The post-run PAUSED branch of `_async_cmd_new`/`_async_cmd_resume` must read
`coord.pause_reason` (default `ctrl-c`) so the journaled reason matches the
originating source."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.cli import main as cli_main
from imp.engine.change import Change
from tests.conftest import _seed_control


def test_new_non_interactive_routes_through_session_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headless `imp new` is a thin session-client: it routes through
    run_session_client (the daemon owns the worktree + lock), binds a headless
    deliver-once solicit from --message, threads the seed, and never runs the
    agent loop in-process nor spawns an ESC reader."""
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    made: dict[str, str | None] = {}

    def _fake_make_headless_solicit(message: str | None, _coord: object) -> Any:
        made["message"] = message

        async def _solicit(_prompt: str) -> str:
            return message or ""

        return _solicit

    monkeypatch.setattr(
        cli_main, "make_headless_solicit", _fake_make_headless_solicit,
        raising=False)

    captured: dict[str, object] = {}

    async def _fake_run_session_client(
        change: Change, *, driver: object, coord: object, solicit: object,
        seed_opening: object = None, status: object = None,
        opening_intent: object = None, leave_mode: object = None,
    ) -> int:
        captured["change_id"] = change.id
        captured["seed_opening"] = seed_opening
        return 7

    monkeypatch.setattr(
        cli_main, "run_session_client", _fake_run_session_client)

    rc = anyio.run(cli_main._async_cmd_new, argparse.Namespace(
        title="ni", non_interactive=True, message="do it"))

    assert rc == 7
    assert made["message"] == "do it"
    assert captured["seed_opening"] is None



def test_new_interactive_routes_through_session_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interactive imp new routes through run_session_client, not run_change."""
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    made: dict[str, object] = {}

    def _fake_make_interactive_solicit(
        change: Change, *, status: object, driver: object, coord: object,
    ) -> Any:
        made["change_id"] = change.id

        async def _solicit(_prompt: str) -> str:
            return ""

        return _solicit

    monkeypatch.setattr(
        cli_main, "make_interactive_solicit", _fake_make_interactive_solicit)

    captured: dict[str, object] = {}

    async def _fake_run_session_client(
        change: Change, *, driver: object, coord: object, solicit: object,
        seed_opening: object = None, status: object = None,
        opening_intent: object = None, leave_mode: object = None,
        leave_state: object = None,
    ) -> int:
        captured["change_id"] = change.id
        captured["seed_opening"] = seed_opening
        return 5

    monkeypatch.setattr(
        cli_main, "run_session_client", _fake_run_session_client)

    orig_in, orig_out = sys.stdin.isatty, sys.stdout.isatty
    sys.stdin.isatty = lambda: True   # type: ignore[method-assign]
    sys.stdout.isatty = lambda: True  # type: ignore[method-assign]
    try:
        rc = anyio.run(cli_main._async_cmd_new, argparse.Namespace(
            title=None, opening="add a thing", non_interactive=False,
            message=None, tui=False, spec_file=None))
    finally:
        sys.stdin.isatty = orig_in    # type: ignore[method-assign]
        sys.stdout.isatty = orig_out  # type: ignore[method-assign]

    assert rc == 5
    assert made["change_id"] == captured["change_id"]   # solicit bound to the same change
    assert captured["seed_opening"] == "add a thing"    # the opening seed threaded through
