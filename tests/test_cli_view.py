# tests/test_cli_view.py
from __future__ import annotations

import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from imp.cli.main import _use_tui, main


# ── Task-17: resume uses _stream_sinks, not ChatRenderer ─────────────────────

def test_replay_current_step_removed() -> None:
    """_replay_current_step must be deleted after Task 17."""
    import imp.cli.main as m
    assert not hasattr(m, "_replay_current_step"), (
        "_replay_current_step was not removed — Task 17 incomplete"
    )


def test_resume_uses_stream_sinks_not_chat_renderer(tmp_path, monkeypatch, capsys) -> None:
    """_async_cmd_resume must route events through _stream_sinks() (stream_event)
    rather than through ChatRenderer.  We patch _stream_sinks to record calls and
    verify at least the workflow_resumed event reaches it.
    """
    import argparse
    import json
    import anyio
    import imp.cli.main as m
    from imp.engine.state import State

    # Build a minimal paused change on disk (without a real workflow).
    change_dir = tmp_path / ".imp" / "changes" / "1"
    (change_dir / "state").mkdir(parents=True)
    (change_dir / "artifacts").mkdir()
    (change_dir / "events.jsonl").write_text("")
    (change_dir / "change.json").write_text(
        json.dumps({"id": 1, "title": "resume-test"})
    )
    # Write paused state
    state = State(change_id=1, status="paused", phase=None, step=None,
                  step_attempt=0, phase_attempt=0)
    state.save(change_dir / "state")

    received: list[dict] = []
    output_lines: list[str] = []

    def fake_stream_sinks():
        def on_event(e: dict) -> None:
            received.append(e)
        def print_text(t: str) -> None:
            output_lines.append(t)
        return on_event, print_text

    # Stub run_change so it immediately returns COMPLETED without calling a real provider.
    from imp.engine.executor import RunResult
    async def fake_run_change(change, **kwargs):
        # Invoke on_event with a dummy event so we can verify the callback.
        on_event = kwargs.get("on_event")
        if on_event:
            on_event({"type": "change_created", "payload": {}})
        return RunResult.COMPLETED

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_stream_sinks", fake_stream_sinks)
    monkeypatch.setattr(m, "run_change", fake_run_change)

    args = argparse.Namespace(change_id=1)
    rc = anyio.run(m._async_cmd_resume, args)

    assert rc == 0
    # The on_event callback from _stream_sinks must have been called.
    types = [e.get("type") for e in received]
    assert "change_created" in types, f"stream_sinks on_event not called; got {types}"


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def test_use_tui_false_when_not_a_tty() -> None:
    assert _use_tui(no_tui=False, stream=_Pipe()) is False


def test_use_tui_false_when_no_tui_flag_even_on_tty() -> None:
    assert _use_tui(no_tui=True, stream=_TTY()) is False


def test_use_tui_true_on_tty_without_flag() -> None:
    assert _use_tui(no_tui=False, stream=_TTY()) is True


def test_log_no_tui_renders_narrative_text(tmp_path, monkeypatch, capsys) -> None:
    cdir = tmp_path / ".imp" / "changes" / "1"
    cdir.mkdir(parents=True)
    (cdir / "change.json").write_text(json.dumps({"id": 1, "title": "demo"}))
    (cdir / "events.jsonl").write_text(
        json.dumps({"id": "e1", "agent_id": None, "type": "change_created",
                    "payload": {"change_id": 1, "title": "demo"}}) + "\n"
        + json.dumps({"id": "e2", "agent_id": None, "type": "phase_started",
                      "payload": {"phase": "design"}}) + "\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)
    rc = main(["log", "1", "--no-tui"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "#1" in out and "demo" in out and "design" in out
    assert "{" not in out          # narrative, not raw json


# add to tests/test_cli_view.py
def test_log_on_tty_degrades_to_text_before_tui_exists(tmp_path, monkeypatch):
    import imp.cli.main as m
    cdir = tmp_path / ".imp" / "changes" / "1"
    cdir.mkdir(parents=True)
    (cdir / "change.json").write_text('{"id": 1, "title": "demo"}')
    (cdir / "events.jsonl").write_text(
        '{"id":"e1","agent_id":null,"type":"change_created",'
        '"payload":{"change_id":1,"title":"demo"}}\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_use_tui", lambda **k: True)        # pretend TTY
    monkeypatch.setattr(m, "_tui_available", lambda: False)     # tui.py absent
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = m.main(["log", "1"])
    assert rc == 0 and "demo" in buf.getvalue()                # text fallback, no ImportError


# ── Task-18: CLI-integration tests migrated from test_cli_log_render.py ──────


def test_cli_log_pipes_through_narrative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """End-to-end: ``imp log`` reads events.jsonl and prints narrative lines.

    Constructs a minimal Change directory by hand (no engine spinup) and
    invokes ``main([..])`` directly.  The narrative view renders
    change_created as the workflow header "#1  <title>" and phase_started as
    the phase name — no raw JSON braces.
    """
    project = tmp_path
    (project / ".imp" / "changes" / "1").mkdir(parents=True)
    (project / ".imp" / "changes" / "1" / "change.json").write_text(
        json.dumps({"id": 1, "title": "smoke"}),
    )
    (project / ".imp" / "changes" / "1" / "events.jsonl").write_text(
        json.dumps({
            "id": "e1", "agent_id": None,
            "timestamp": "2026-05-27T10:00:00+00:00",
            "type": "change_created",
            "payload": {"change_id": 1, "title": "smoke"},
        }) + "\n"
        + json.dumps({
            "id": "e2", "agent_id": None,
            "timestamp": "2026-05-27T10:00:01+00:00",
            "type": "phase_started",
            "payload": {"phase": "onboard"},
        }) + "\n",
    )
    monkeypatch.chdir(project)
    monkeypatch.delenv("NO_COLOR", raising=False)

    rc = main(["log", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    # Narrative header: "#1  smoke"
    assert "#1" in out
    assert "smoke" in out
    # Phase rendered by name
    assert "onboard" in out
    # No raw JSON braces in narrative rendering
    assert "{" not in out


def test_imp_log_skips_malformed_lines_in_events_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A garbage line in events.jsonl renders as a malformed error line;
    the surrounding good events still appear and the command exits 0."""
    cdir = tmp_path / ".imp" / "changes" / "1"
    cdir.mkdir(parents=True)
    (cdir / "change.json").write_text(json.dumps({"id": 1, "title": "smoke"}))
    (cdir / "events.jsonl").write_text(
        json.dumps({
            "id": "e1", "agent_id": None,
            "timestamp": "2026-05-27T10:00:00+00:00",
            "type": "change_created",
            "payload": {"change_id": 1, "title": "smoke"},
        }) + "\n"
        + "this is not json\n"
        + json.dumps({
            "id": "e2", "agent_id": None,
            "timestamp": "2026-05-27T10:00:02+00:00",
            "type": "phase_started",
            "payload": {"phase": "onboard"},
        }) + "\n",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["log", "1"])
    assert rc == 0
    out = buf.getvalue()
    # Good events still rendered
    assert "#1" in out and "smoke" in out
    assert "onboard" in out
    # Malformed line rendered as an error line (narrative ErrorNode)
    assert "malformed event" in out


def test_cli_log_subprocess_no_color_on_pipe(tmp_path: Path) -> None:
    """`python -m imp log 1` against a real change dir, stdout piped → no ANSI.

    Covers the ``__main__.py → _cmd_log`` wiring, ``should_use_color`` on a
    non-tty stream, and the real ``sys.exit`` path.  The narrative view renders
    change_created as "#1  <title>" and phase_started as the phase name."""

    cdir = tmp_path / ".imp" / "changes" / "1"
    cdir.mkdir(parents=True)
    (cdir / "change.json").write_text(json.dumps({"id": 1, "title": "sub"}))
    (cdir / "events.jsonl").write_text(
        json.dumps({
            "id": "e1", "agent_id": None,
            "timestamp": "2026-05-27T10:00:00+00:00",
            "type": "change_created",
            "payload": {"change_id": 1, "title": "sub"},
        }) + "\n"
        + json.dumps({
            "id": "e2", "agent_id": None,
            "timestamp": "2026-05-27T10:00:01+00:00",
            "type": "phase_started",
            "payload": {"phase": "spec"},
        }) + "\n",
    )

    result = subprocess.run(
        [sys.executable, "-m", "imp", "log", "1"],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, (
        f"stderr: {result.stderr!r}\nstdout: {result.stdout!r}"
    )
    # Narrative header: "#1  sub", phase: "spec"
    assert "#1" in result.stdout and "sub" in result.stdout
    assert "spec" in result.stdout
    # Pipe → not a tty → no ANSI codes regardless of NO_COLOR.
    assert "\x1b[" not in result.stdout


def test_cmd_log_auto_picks_latest_change_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`imp log` (no id) picks the highest numeric change dir."""
    for cid, title in [(1, "first"), (2, "second"), (10, "tenth")]:
        cdir = tmp_path / ".imp" / "changes" / str(cid)
        cdir.mkdir(parents=True)
        (cdir / "change.json").write_text(
            json.dumps({"id": cid, "title": title}),
        )
        (cdir / "events.jsonl").write_text(
            json.dumps({
                "id": "e1", "agent_id": None,
                "timestamp": "2026-05-27T10:00:00+00:00",
                "type": "change_created",
                "payload": {"change_id": cid, "title": title},
            }) + "\n",
        )
    monkeypatch.chdir(tmp_path)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["log"])
    assert rc == 0
    out = buf.getvalue()
    # 10 is the highest, so its title should render.
    assert "tenth" in out
    assert "first" not in out
    assert "second" not in out


def test_cmd_log_no_changes_yet_returns_rc_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """`imp log` in a project with no .imp/changes/ returns rc=2 with a
    friendly stderr message."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".imp").mkdir()
    rc = main(["log"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no Changes" in err
