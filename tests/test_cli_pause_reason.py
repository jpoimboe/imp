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


def test_new_spawns_esc_reader_as_taskgroup_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interactive `_async_cmd_new` starts an EscKeyReader.run task."""
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
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
    rc = anyio.run(cli_main._async_cmd_new, argparse.Namespace(title="sib"))
    assert rc == 0
    assert seen == ["esc_reader.run"]


def test_new_non_interactive_does_not_spawn_esc_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    seen: list[str] = []

    class _RecordingReader:
        def __init__(self, _coord: object) -> None: ...
        async def run(self) -> None:
            seen.append("esc_reader.run")

    monkeypatch.setattr(cli_main, "EscKeyReader", _RecordingReader)

    async def _fake_run_change(_change: object, **_kw: object) -> RunResult:
        return RunResult.COMPLETED

    monkeypatch.setattr(cli_main, "run_change", _fake_run_change)
    rc = anyio.run(cli_main._async_cmd_new, argparse.Namespace(
        title="ni", non_interactive=True, message=None))
    assert rc == 0
    assert seen == []


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
