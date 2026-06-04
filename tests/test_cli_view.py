# tests/test_cli_view.py
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from imp.cli.main import main

# ── resume renders through LiveDriver (no replay) ────────────────────────────

def test_replay_current_step_removed() -> None:
    """_replay_current_step must be deleted after Task 17."""
    import imp.cli.main as m
    assert not hasattr(m, "_replay_current_step"), (
        "_replay_current_step was not removed — Task 17 incomplete"
    )


def _make_change_worktree(control: Path, slug: str, title: str,
                           status: str = "paused") -> Path:
    """Create a minimal on-disk worktree for a Change (no real git worktree
    needed — Change.load only reads the .imp/change/ directory)."""
    from imp.engine.state import State

    wt = control / ".imp" / "worktrees" / slug
    change_dir = wt / ".imp" / "change"
    change_dir.mkdir(parents=True)
    (change_dir / "state").mkdir()
    (change_dir / "change.json").write_text(
        json.dumps({"slug": slug, "title": title, "base_branch": "main"})
    )
    (change_dir / "events.jsonl").write_text("")
    state = State(slug=slug, status=status, step="spec", step_attempt=0)
    state.save(change_dir / "state")
    return wt


def test_resume_renders_through_live_driver_without_replay(tmp_path, monkeypatch) -> None:
    """_async_cmd_resume routes events through LiveDriver (not _stream_sinks) and
    does NOT replay events_for_current_step — the re-run emits fresh."""
    import argparse

    import anyio

    import imp.cli.main as m
    from imp.engine.executor import RunResult
    from imp.engine.state import State

    wt = _make_change_worktree(tmp_path, "resume-test", "resume-test", status="paused")
    change_dir = wt / ".imp" / "change"

    async def fake_run_change(change, **kwargs):
        on_event = kwargs["on_event"]
        on_event({"id": "r2", "agent_id": None, "type": "step_started",
                  "payload": {"step": "spec", "kind": "agent", "phase": "design",
                              "model": "m", "provider": "anthropic"}})
        return RunResult.COMPLETED

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "run_change", fake_run_change)

    buf = io.StringIO()
    args = argparse.Namespace(slug="resume-test")
    with redirect_stdout(buf):
        rc = anyio.run(m._async_cmd_resume, args)
    out = buf.getvalue()
    assert rc == 0
    # The step header is rendered exactly once — no replay duplicate. Match the
    # rendered header ("  spec" -> strip == "spec"), NOT the "Phase: design,
    # step: spec" banner (whose strip ends with "spec" but isn't equal to it).
    step_lines = [ln for ln in out.splitlines() if ln.strip() == "spec"]
    assert len(step_lines) == 1

    # Side-effect assertions: workflow_resumed was appended to events.jsonl.
    events_path = change_dir / "events.jsonl"
    event_types = [
        json.loads(line)["type"]
        for line in events_path.read_text().splitlines()
        if line.strip()
    ]
    assert "workflow_resumed" in event_types, (
        f"workflow_resumed not in events; found: {event_types}"
    )

    # Side-effect assertions: on-disk state status was flipped to "running".
    state = State.load(change_dir / "state")
    assert state.status == "running", (
        f"expected status='running' after resume, got {state.status!r}"
    )


def test_resume_accepts_merge_blocked_status(tmp_path, monkeypatch) -> None:
    """_async_cmd_resume must NOT early-return for status='merge_blocked'.

    The status guard allows 'paused', 'errored', and 'merge_blocked'.  This
    test verifies merge_blocked is treated as resumable: run_change is called
    and the command exits 0 (not the status-rejection rc=2 path)."""
    import argparse

    import anyio

    import imp.cli.main as m
    from imp.engine.executor import RunResult

    _make_change_worktree(tmp_path, "mb-test", "mb-test", status="merge_blocked")

    stub_called = []

    async def fake_run_change(change, **kwargs):
        stub_called.append(True)
        on_event = kwargs["on_event"]
        on_event({"id": "r1", "agent_id": None, "type": "step_started",
                  "payload": {"step": "merge", "kind": "agent", "phase": "ship",
                              "model": "m", "provider": "anthropic"}})
        return RunResult.COMPLETED

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(m, "run_change", fake_run_change)

    buf = io.StringIO()
    args = argparse.Namespace(slug="mb-test")
    with redirect_stdout(buf):
        rc = anyio.run(m._async_cmd_resume, args)

    # Must NOT hit the status-rejection path (rc=2 + "nothing to resume").
    assert rc == 0, (
        f"merge_blocked was rejected by the status guard (rc={rc}); "
        "merge_blocked must be resumable"
    )
    assert stub_called, "run_change was never invoked — merge_blocked was rejected early"


# ── Task-18: CLI-integration tests migrated from test_cli_log_render.py ──────


def test_cli_log_pipes_through_narrative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """End-to-end: ``imp log`` reads events.jsonl and prints narrative lines.

    Constructs a minimal Change directory by hand (no engine spinup) and
    invokes ``main([..])`` directly.  The narrative view renders
    change_created as the workflow header "#<slug>  <title>" and phase_started
    as the phase name — no raw JSON braces.
    """
    import imp.cli.main as m

    slug = "my-smoke"
    wt = tmp_path / ".imp" / "worktrees" / slug
    change_dir = wt / ".imp" / "change"
    change_dir.mkdir(parents=True)
    (change_dir / "change.json").write_text(
        json.dumps({"slug": slug, "title": "smoke", "base_branch": "main"}),
    )
    (change_dir / "events.jsonl").write_text(
        json.dumps({
            "id": "e1", "agent_id": None,
            "timestamp": "2026-05-27T10:00:00+00:00",
            "type": "change_created",
            "payload": {"slug": slug, "title": "smoke"},
        }) + "\n"
        + json.dumps({
            "id": "e2", "agent_id": None,
            "timestamp": "2026-05-27T10:00:01+00:00",
            "type": "step_started",
            "payload": {"step": "s", "kind": "agent", "phase": "onboard",
                        "model": "m", "provider": "anthropic"},
        }) + "\n",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)

    rc = main(["log", slug])
    assert rc == 0
    out = capsys.readouterr().out
    # Narrative header contains the title
    assert "smoke" in out
    # Phase rendered by name (derived from step_started.payload.phase)
    assert "onboard" in out
    # No raw JSON braces in narrative rendering
    assert "{" not in out


def test_imp_log_skips_malformed_lines_in_events_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A garbage line in events.jsonl renders as a malformed error line;
    the surrounding good events still appear and the command exits 0."""
    import imp.cli.main as m

    slug = "my-smoke"
    wt = tmp_path / ".imp" / "worktrees" / slug
    change_dir = wt / ".imp" / "change"
    change_dir.mkdir(parents=True)
    (change_dir / "change.json").write_text(
        json.dumps({"slug": slug, "title": "smoke", "base_branch": "main"}),
    )
    (change_dir / "events.jsonl").write_text(
        json.dumps({
            "id": "e1", "agent_id": None,
            "timestamp": "2026-05-27T10:00:00+00:00",
            "type": "change_created",
            "payload": {"slug": slug, "title": "smoke"},
        }) + "\n"
        + "this is not json\n"
        + json.dumps({
            "id": "e2", "agent_id": None,
            "timestamp": "2026-05-27T10:00:02+00:00",
            "type": "step_started",
            "payload": {"step": "s", "kind": "agent", "phase": "onboard",
                        "model": "m", "provider": "anthropic"},
        }) + "\n",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["log", slug])
    assert rc == 0
    out = buf.getvalue()
    # Good events still rendered
    assert "smoke" in out
    assert "onboard" in out
    # Malformed line rendered as an error line (narrative ErrorNode)
    assert "malformed event" in out


def test_cli_log_subprocess_no_color_on_pipe(tmp_path: Path) -> None:
    """`python -m imp log <slug>` against a real change dir, stdout piped → no ANSI.

    Covers the ``__main__.py → _cmd_log`` wiring, ``should_use_color`` on a
    non-tty stream, and the real ``sys.exit`` path.  The narrative view renders
    change_created and phase_started as the phase name."""
    from tests.conftest import _seed_control

    _seed_control(tmp_path)

    slug = "my-sub"
    wt = tmp_path / ".imp" / "worktrees" / slug
    change_dir = wt / ".imp" / "change"
    change_dir.mkdir(parents=True)
    (change_dir / "change.json").write_text(
        json.dumps({"slug": slug, "title": "sub", "base_branch": "main"}),
    )
    (change_dir / "events.jsonl").write_text(
        json.dumps({
            "id": "e1", "agent_id": None,
            "timestamp": "2026-05-27T10:00:00+00:00",
            "type": "change_created",
            "payload": {"slug": slug, "title": "sub"},
        }) + "\n"
        + json.dumps({
            "id": "e2", "agent_id": None,
            "timestamp": "2026-05-27T10:00:01+00:00",
            "type": "step_started",
            "payload": {"step": "s", "kind": "agent", "phase": "spec",
                        "model": "m", "provider": "anthropic"},
        }) + "\n",
    )

    result = subprocess.run(
        [sys.executable, "-m", "imp", "log", slug],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, (
        f"stderr: {result.stderr!r}\nstdout: {result.stdout!r}"
    )
    # Narrative header: title "sub", phase: "spec"
    assert "sub" in result.stdout
    assert "spec" in result.stdout
    # Pipe → not a tty → no ANSI codes regardless of NO_COLOR.
    assert "\x1b[" not in result.stdout


def test_main_swallows_broken_pipe(monkeypatch) -> None:
    """A reader closing the pipe early (e.g. `imp log | less` quit) must not
    surface a BrokenPipeError traceback: main() catches it and returns the
    conventional 128+SIGPIPE status. os.dup2/os.open are stubbed so the real
    handler's stdout-fd redirect can't clobber pytest's own stdout."""
    import signal

    import imp.cli.main as m

    def boom(_ns: object) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(m, "_cmd_status", boom)
    monkeypatch.setattr(m.os, "open", lambda *a, **k: 0)
    monkeypatch.setattr(m.os, "dup2", lambda *a, **k: None)

    rc = m.main(["status"])
    assert rc == 128 + signal.SIGPIPE


def test_cmd_log_auto_picks_latest_by_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`imp log` (no slug) picks the most-recently-modified worktree's events.jsonl."""
    import imp.cli.main as m

    slugs_titles = [("first-change", "first"), ("second-change", "second"),
                    ("tenth-change", "tenth")]
    for slug, title in slugs_titles:
        wt = tmp_path / ".imp" / "worktrees" / slug
        change_dir = wt / ".imp" / "change"
        change_dir.mkdir(parents=True)
        (change_dir / "change.json").write_text(
            json.dumps({"slug": slug, "title": title, "base_branch": "main"}),
        )
        events_path = change_dir / "events.jsonl"
        events_path.write_text(
            json.dumps({
                "id": "e1", "agent_id": None,
                "timestamp": "2026-05-27T10:00:00+00:00",
                "type": "change_created",
                "payload": {"slug": slug, "title": title},
            }) + "\n",
        )
        # Stagger mtimes so the last one is clearly newest
        time.sleep(0.01)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["log"])
    assert rc == 0
    out = buf.getvalue()
    # tenth-change was written last, so its title should render.
    assert "tenth" in out
    assert "first" not in out
    assert "second" not in out


def test_cmd_log_no_changes_yet_returns_rc_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """`imp log` in a project with no .imp/worktrees/ returns rc=2 with a
    friendly stderr message."""
    import imp.cli.main as m

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    (tmp_path / ".imp").mkdir()
    rc = main(["log"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "in-flight" in err.lower() or "no" in err.lower()


def test_new_renders_through_live_driver(tmp_path, monkeypatch) -> None:
    """imp new routes events through LiveDriver: a stubbed run_change that emits
    a phase renders the phase name via the folded renderer (no raw JSON)."""
    import argparse

    import anyio

    import imp.cli.main as m
    from imp.engine.executor import RunResult
    from tests.conftest import _seed_control

    # _seed_control initialises a git repo and commits imp/workflows/default +
    # imp/config.yaml — the layout Change.create now expects.
    _seed_control(tmp_path)

    async def fake_run_change(change, **kwargs):
        on_event = kwargs["on_event"]
        on_event({"id": "1", "agent_id": None, "type": "change_created",
                  "payload": {"slug": change.slug, "title": change.title}})
        on_event({"id": "2", "agent_id": None, "type": "step_started",
                  "payload": {"step": "s", "kind": "agent", "phase": "design",
                              "model": "x", "provider": "anthropic"}})
        return RunResult.COMPLETED

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "run_change", fake_run_change)

    buf = io.StringIO()
    args = argparse.Namespace(title="demo")
    with redirect_stdout(buf):
        rc = anyio.run(m._async_cmd_new, args)
    out = buf.getvalue()
    assert rc == 0
    assert "#" in out and "design" in out
    assert "{" not in out          # narrative, not raw json


def test_term_width_floors_to_80_when_columns_nonpositive(monkeypatch) -> None:
    from imp.cli import main as cli_main
    monkeypatch.delenv("COLUMNS", raising=False)
    # Inner query reports 0 columns (winsize-0 PTY); shutil's `or fallback[0]`
    # must substitute 80.
    monkeypatch.setattr(os, "get_terminal_size", lambda fd: os.terminal_size((0, 24)))
    assert cli_main._term_width() == 80
