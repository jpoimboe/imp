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
from imp.cli.signals import InterruptCoordinator
from imp.engine.change import Change
from imp.engine.executor import RunResult
from tests.conftest import _seed_control


@pytest.mark.parametrize(
    "pause_reason",
    ["ctrl-c", "esc", "sigterm", None],
)
def test_resume_paused_journals_correct_pause_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pause_reason: str | None,
) -> None:
    from imp.engine.state import State

    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)
    monkeypatch.setattr(cli_main, "_control_root", lambda: tmp_path)

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

    rc = anyio.run(cli_main._async_cmd_resume, argparse.Namespace(
        token="feat", from_target=None, force=False))

    assert rc == 130
    assert reasons == [pause_reason or "ctrl-c"]


def test_new_non_interactive_routes_through_session_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headless `imp new` is a thin session-client: it routes through
    run_session_client (the daemon owns the worktree + lock), binds a headless
    deliver-once solicit from --message, threads the seed, and never runs the
    agent loop in-process nor spawns an ESC reader."""
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    seen: list[str] = []

    class _RecordingReader:
        def __init__(self, _coord: object) -> None: ...
        async def run(self) -> None:
            seen.append("esc_reader.run")

    monkeypatch.setattr(cli_main, "EscKeyReader", _RecordingReader)

    async def _fail_run_change(*_a: object, **_k: object) -> object:
        raise AssertionError("run_change must not run in-process for headless new")

    monkeypatch.setattr(cli_main, "run_change", _fail_run_change)

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
    ) -> int:
        captured["change_id"] = change.id
        captured["seed_opening"] = seed_opening
        return 7

    monkeypatch.setattr(
        cli_main, "run_session_client", _fake_run_session_client)

    rc = anyio.run(cli_main._async_cmd_new, argparse.Namespace(
        title="ni", non_interactive=True, message="do it"))

    assert rc == 7
    assert seen == []
    assert made["message"] == "do it"
    assert captured["seed_opening"] is None


def test_resume_spawns_esc_reader_as_taskgroup_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interactive `_async_cmd_resume` also starts an EscKeyReader.run task —
    parity with `_async_cmd_new` (the wiring is mirrored across both commands)."""
    from imp.engine.state import State

    _seed_control(tmp_path)
    ch = Change.create(tmp_path, title="x", change_id="feat", base_branch="main")
    st = State.load(ch.state_dir)
    st.status = "paused"
    st.save(ch.state_dir)
    monkeypatch.setattr(cli_main, "_control_root", lambda: tmp_path)

    seen: list[str] = []
    real = cli_main.EscKeyReader  # type: ignore[attr-defined]

    class _RecordingReader(real):              # type: ignore[misc, valid-type]
        async def run(
            self, *, task_status: object = anyio.TASK_STATUS_IGNORED,
        ) -> None:
            seen.append("esc_reader.run")
            task_status.started()  # type: ignore[attr-defined]
            return

    monkeypatch.setattr(cli_main, "EscKeyReader", _RecordingReader)

    async def _fake_run_change(_change: object, **_kw: object) -> RunResult:
        return RunResult.COMPLETED

    monkeypatch.setattr(cli_main, "run_change", _fake_run_change)
    rc = anyio.run(cli_main._async_cmd_resume, argparse.Namespace(
        token="feat", from_target=None, force=False))
    assert rc == 0
    assert seen == ["esc_reader.run"]


def test_new_interactive_routes_through_session_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interactive imp new routes through run_session_client, not run_change."""
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)

    seen: list[str] = []

    class _RecordingReader:
        def __init__(self, _coord: object) -> None: ...

        async def run(self, *, task_status: object = anyio.TASK_STATUS_IGNORED) -> None:
            seen.append("esc_reader.run")
            task_status.started()  # type: ignore[attr-defined]

    monkeypatch.setattr(cli_main, "EscKeyReader", _RecordingReader)

    async def _fail_run_change(*_a: object, **_k: object) -> object:
        raise AssertionError("run_change must not run in-process for interactive new")

    monkeypatch.setattr(cli_main, "run_change", _fail_run_change)

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
    assert seen == []                                   # no ESC reader spawned
    assert made["change_id"] == captured["change_id"]   # solicit bound to the same change
    assert captured["seed_opening"] == "add a thing"    # the opening seed threaded through
