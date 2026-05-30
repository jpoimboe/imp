# tests/test_cli_view.py
from __future__ import annotations

import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from imp.cli.main import main
from tests.conftest import write_agent_md

# ── resume renders through LiveDriver (no replay) ────────────────────────────

def test_replay_current_step_removed() -> None:
    """_replay_current_step must be deleted after Task 17."""
    import imp.cli.main as m
    assert not hasattr(m, "_replay_current_step"), (
        "_replay_current_step was not removed — Task 17 incomplete"
    )


def test_resume_renders_through_live_driver_without_replay(tmp_path, monkeypatch) -> None:
    """_async_cmd_resume routes events through LiveDriver (not _stream_sinks) and
    does NOT replay events_for_current_step — the re-run emits fresh."""
    import argparse
    import json

    import anyio

    import imp.cli.main as m
    from imp.engine.executor import RunResult
    from imp.engine.state import State

    change_dir = tmp_path / ".imp" / "changes" / "1"
    (change_dir / "state").mkdir(parents=True)
    (change_dir / "artifacts").mkdir()
    (change_dir / "events.jsonl").write_text("")
    (change_dir / "change.json").write_text(json.dumps({"id": 1, "title": "resume-test"}))
    state = State(change_id=1, status="paused", phase="design", step="spec",
                  step_attempt=0, phase_attempt=0)
    state.save(change_dir / "state")

    async def fake_run_change(change, **kwargs):
        on_event = kwargs["on_event"]
        on_event({"id": "r1", "agent_id": None, "type": "phase_started",
                  "payload": {"phase": "design"}})
        on_event({"id": "r2", "agent_id": None, "type": "step_started",
                  "payload": {"step": "spec", "model": "m", "provider": "anthropic"}})
        return RunResult.COMPLETED

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "run_change", fake_run_change)

    buf = io.StringIO()
    args = argparse.Namespace(change_id=1)
    with redirect_stdout(buf):
        rc = anyio.run(m._async_cmd_resume, args)
    out = buf.getvalue()
    assert rc == 0
    # The step header is rendered exactly once — no replay duplicate. Match the
    # rendered header ("  spec" -> strip == "spec"), NOT the "Phase: design,
    # step: spec" banner (whose strip ends with "spec" but isn't equal to it).
    step_lines = [ln for ln in out.splitlines() if ln.strip() == "spec"]
    assert len(step_lines) == 1


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


def test_new_renders_through_live_driver(tmp_path, monkeypatch) -> None:
    """imp new routes events through LiveDriver: a stubbed run_change that emits
    a phase renders the phase name via the folded renderer (no raw JSON)."""
    import argparse

    import anyio

    import imp.cli.main as m
    from imp.engine.executor import RunResult

    project = tmp_path
    (project / ".imp").mkdir()
    # Change.create snapshots+validates a default workflow before run_change
    # runs, so seed a minimal valid one.
    src = project / ".imp" / "workflows" / "default"
    (src / "agents").mkdir(parents=True)
    write_agent_md(
        src / "agents" / "stub.md",
        frontmatter=(
            "name: stub\ntools: [complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    (src / "workflow.yaml").write_text(
        "name: t\ndescription: \"\"\nphases: [p]\n"
        "p:\n  steps:\n    - {name: s, file: agents/stub.md}\n"
    )
    (project / ".imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: [{provider: anthropic, model: x}]}\n"
    )

    async def fake_run_change(change, **kwargs):
        on_event = kwargs["on_event"]
        on_event({"id": "1", "agent_id": None, "type": "change_created",
                  "payload": {"change_id": change.id, "title": change.title}})
        on_event({"id": "2", "agent_id": None, "type": "phase_started",
                  "payload": {"phase": "design"}})
        return RunResult.COMPLETED

    monkeypatch.chdir(project)
    monkeypatch.setattr(m, "run_change", fake_run_change)

    buf = io.StringIO()
    args = argparse.Namespace(title="demo")
    with redirect_stdout(buf):
        rc = anyio.run(m._async_cmd_new, args)
    out = buf.getvalue()
    assert rc == 0
    assert "#" in out and "design" in out
    assert "{" not in out          # narrative, not raw json
