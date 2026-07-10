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
from imp.engine.change import LAYOUT_VERSION

# ── resume renders through LiveDriver (no replay) ────────────────────────────

def test_replay_current_step_removed() -> None:
    """_replay_current_step must be deleted after Task 17."""
    import imp.cli.main as m
    assert not hasattr(m, "_replay_current_step"), (
        "_replay_current_step was not removed — Task 17 incomplete"
    )


def _make_change_worktree(control: Path, quest_id: str, title: str,
                           status: str = "paused") -> Path:
    """Create a minimal on-disk Change home (no real git worktree needed —
    Change.load only reads the permanent .imp/changes/<id>/ directory)."""
    from imp.engine.state import State

    quest_dir = control / ".imp" / "changes" / quest_id
    quest_dir.mkdir(parents=True)
    (quest_dir / "state").mkdir()
    (quest_dir / "change.json").write_text(
        json.dumps({"id": quest_id, "title": title, "base_branch": "main",
                    "layout_version": LAYOUT_VERSION})
    )
    (quest_dir / "events.jsonl").write_text("")
    state = State(id=quest_id, status=status, step="spec", step_attempt=0)
    state.save(quest_dir / "state")
    return quest_dir


def test_resume_accepts_merge_blocked_status(tmp_path, monkeypatch) -> None:
    """The unlocked status precondition treats 'merge_blocked' as resumable."""
    import argparse

    import anyio

    import imp.cli.main as m

    _make_change_worktree(tmp_path, "mb-test", "mb-test", status="merge_blocked")

    called: list[str] = []

    async def _fake_run_session_client(change, **kwargs) -> int:
        called.append(change.id)
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(
        m, "make_interactive_solicit", lambda *a, **k: (lambda _p: None))
    monkeypatch.setattr(m, "run_session_client", _fake_run_session_client)

    args = argparse.Namespace(token="mb-test")
    rc = anyio.run(m._async_cmd_resume, args)

    assert rc == 0
    assert called == ["mb-test"]    # passed to the thin client, not rejected early


def test_resume_accepts_crashed_at_rest_status(tmp_path, monkeypatch) -> None:
    """A crashed/at-rest run is resumable; the resume guard must not refuse it."""
    import argparse

    import anyio

    import imp.cli.main as m

    _make_change_worktree(tmp_path, "crashed", "crashed", status="new")

    called: list[str] = []

    async def _fake_run_session_client(change, **kwargs) -> int:
        called.append(change.id)
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(
        m, "make_interactive_solicit", lambda *a, **k: (lambda _p: None))
    monkeypatch.setattr(m, "run_session_client", _fake_run_session_client)

    args = argparse.Namespace(token="crashed")
    rc = anyio.run(m._async_cmd_resume, args)

    assert rc == 0
    assert called == ["crashed"]    # the 'running' guard did not refuse it


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
    quest_dir = tmp_path / ".imp" / "changes" / slug
    quest_dir.mkdir(parents=True)
    (quest_dir / "change.json").write_text(
        json.dumps({"id": slug, "title": "smoke", "base_branch": "main",
                    "layout_version": LAYOUT_VERSION}),
    )
    (quest_dir / "events.jsonl").write_text(
        json.dumps({
            "id": "e1", "agent_id": None,
            "timestamp": "2026-05-27T10:00:00+00:00",
            "type": "change_created",
            "payload": {"title": "smoke"},
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
    # No slug/title banner — the transcript opens on the first phase.
    assert "smoke" not in out
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
    quest_dir = tmp_path / ".imp" / "changes" / slug
    quest_dir.mkdir(parents=True)
    (quest_dir / "change.json").write_text(
        json.dumps({"id": slug, "title": "smoke", "base_branch": "main",
                    "layout_version": LAYOUT_VERSION}),
    )
    (quest_dir / "events.jsonl").write_text(
        json.dumps({
            "id": "e1", "agent_id": None,
            "timestamp": "2026-05-27T10:00:00+00:00",
            "type": "change_created",
            "payload": {"title": "smoke"},
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
    # Good events still rendered (no title banner; the phase carries the proof)
    assert "smoke" not in out
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
    quest_dir = tmp_path / ".imp" / "changes" / slug
    quest_dir.mkdir(parents=True)
    (quest_dir / "change.json").write_text(
        json.dumps({"id": slug, "title": "sub", "base_branch": "main",
                    "layout_version": LAYOUT_VERSION}),
    )
    (quest_dir / "events.jsonl").write_text(
        json.dumps({
            "id": "e1", "agent_id": None,
            "timestamp": "2026-05-27T10:00:00+00:00",
            "type": "change_created",
            "payload": {"title": "sub"},
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
    # No title banner; the phase name is the proof the narrative rendered.
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

    monkeypatch.setattr(m, "_cmd_show", boom)
    monkeypatch.setattr(m.os, "open", lambda *a, **k: 0)
    monkeypatch.setattr(m.os, "dup2", lambda *a, **k: None)

    rc = m.main(["show"])
    assert rc == 128 + signal.SIGPIPE


def test_main_swallows_broken_pipe_on_buffered_flush(monkeypatch) -> None:
    """The command can return cleanly while output sits in stdout's buffer; the
    broken pipe then surfaces only on flush. main() flushes inside its guard so
    that case is caught too, instead of escaping to interpreter shutdown as an
    uncatchable 'Exception ignored' traceback."""
    import signal

    import imp.cli.main as m

    def quiet(_ns: object) -> int:
        return 0  # returns normally; nothing written yet

    class FakeStdout:
        def flush(self) -> None:
            raise BrokenPipeError(32, "Broken pipe")

        def fileno(self) -> int:
            return 1

    monkeypatch.setattr(m, "_cmd_show", quiet)
    monkeypatch.setattr(m.sys, "stdout", FakeStdout())
    monkeypatch.setattr(m.os, "open", lambda *a, **k: 0)
    monkeypatch.setattr(m.os, "dup2", lambda *a, **k: None)

    rc = m.main(["show"])
    assert rc == 128 + signal.SIGPIPE


def test_cmd_log_auto_picks_latest_by_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`imp log` (no slug) picks the most-recently-modified worktree's events.jsonl."""
    import imp.cli.main as m

    slugs_titles = [("first-change", "first"), ("second-change", "second"),
                    ("tenth-change", "tenth")]
    for slug, title in slugs_titles:
        quest_dir = tmp_path / ".imp" / "changes" / slug
        quest_dir.mkdir(parents=True)
        (quest_dir / "change.json").write_text(
            json.dumps({"id": slug, "title": title, "base_branch": "main",
                        "layout_version": LAYOUT_VERSION}),
        )
        events_path = quest_dir / "events.jsonl"
        events_path.write_text(
            json.dumps({
                "id": "e1", "agent_id": None,
                "timestamp": "2026-05-27T10:00:00+00:00",
                "type": "change_created",
                "payload": {"title": title},
            }) + "\n"
            # A step renders the phase name (the title banner is gone), so use a
            # per-change phase name to prove WHICH change's log was picked.
            + json.dumps({
                "id": "e2", "agent_id": None,
                "timestamp": "2026-05-27T10:00:01+00:00",
                "type": "step_started",
                "payload": {"step": "s", "kind": "agent", "phase": title,
                            "model": "m", "provider": "anthropic"},
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
    # tenth-change was written last, so its phase should render.
    assert "tenth" in out
    assert "first" not in out
    assert "second" not in out


def test_cmd_log_no_changes_yet_returns_rc_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """`imp log` in a project with no .imp/worktrees/ returns rc=0 with a
    friendly stdout message."""
    import imp.cli.main as m

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    (tmp_path / ".imp").mkdir()
    rc = main(["log"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "No changes" in captured.out
    assert captured.err == ""


def test_term_width_floors_to_80_when_columns_nonpositive(monkeypatch) -> None:
    from imp.cli import main as cli_main

    class _Tty:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 1

    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setattr(cli_main.sys, "stdout", _Tty())
    # Inner query reports 0 columns (winsize-0 PTY); shutil's `or fallback[0]`
    # must substitute 80.
    monkeypatch.setattr(os, "get_terminal_size", lambda fd: os.terminal_size((0, 24)))
    assert cli_main._term_width() == 80


def test_term_width_honors_explicit_columns_even_when_piped(monkeypatch) -> None:
    from imp.cli import main as cli_main

    class _Pipe:
        def isatty(self) -> bool:
            return False

    monkeypatch.setenv("COLUMNS", "10000")
    monkeypatch.setattr(cli_main.sys, "stdout", _Pipe())
    assert cli_main._term_width() == 10000


def test_term_width_no_wrap_sentinel_when_piped_and_columns_unset(monkeypatch) -> None:
    from imp.cli import main as cli_main

    class _Pipe:
        def isatty(self) -> bool:
            return False

    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setattr(cli_main.sys, "stdout", _Pipe())
    # Piped, no explicit COLUMNS => a large no-wrap width, well above any real terminal.
    assert cli_main._term_width() >= 10000


def test_term_width_uses_real_columns_on_tty_when_columns_unset(monkeypatch) -> None:
    from imp.cli import main as cli_main

    class _Tty:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 1

    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setattr(cli_main.sys, "stdout", _Tty())
    monkeypatch.setattr(os, "get_terminal_size", lambda fd: os.terminal_size((123, 24)))
    assert cli_main._term_width() == 123


def test_log_backlog_id_renders_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    import imp.cli.main as m
    from imp.engine.change import Change
    from tests.conftest import _seed_control
    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    Change.create_draft(
        tmp_path, title="Add OAuth", quest_id="2026-06-16-bkg111",
        base_branch="main", origin="followup",
        seed_opening="Add OAuth\n\nwire up OAuth login")
    rc = main(["log", "2026-06-16-bkg111"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Add OAuth" in out
    assert "wire up OAuth login" in out
    assert "no such Change" not in out


def test_log_picker_rows_include_backlog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.cli.main as m
    from imp.engine.change import Change
    from tests.conftest import _seed_control
    _seed_control(tmp_path)
    Change.create_draft(
        tmp_path, title="Add OAuth", quest_id="2026-06-16-bkg111",
        base_branch="main", origin="idea", seed_opening="Add OAuth")
    captured: dict = {}

    async def _fake_picker(*, control, rows, header, on_empty, selected_index):
        captured["rows"] = rows
        return 2

    monkeypatch.setattr(m, "_pick_change_target", _fake_picker)
    import anyio
    anyio.run(m._pick_log_target, tmp_path)
    assert any(r[0] == "2026-06-16-bkg111" for r in captured["rows"])


def test_display_width_uses_kernel_winsize_ignoring_columns(monkeypatch) -> None:
    from imp.cli import main as cli_main

    class _Out:
        def fileno(self) -> int:
            return 1

    # An inflated COLUMNS must be ignored: _display_width asks the kernel.
    monkeypatch.setenv("COLUMNS", "10000")
    monkeypatch.setattr(cli_main.sys, "stdout", _Out())
    monkeypatch.setattr(os, "get_terminal_size", lambda fd: os.terminal_size((137, 40)))
    assert cli_main._display_width() == 137


def test_display_width_falls_back_to_80_when_no_terminal(monkeypatch) -> None:
    from imp.cli import main as cli_main

    class _Out:
        def fileno(self) -> int:
            return 1

    def _raise(fd):
        raise OSError("not a terminal")

    monkeypatch.setattr(cli_main.sys, "stdout", _Out())
    monkeypatch.setattr(os, "get_terminal_size", _raise)
    assert cli_main._display_width() == 80


def test_log_uses_display_width_for_hr_and_term_width_for_wrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    import imp.cli.main as m

    slug = "wide-demo"
    quest_dir = tmp_path / ".imp" / "changes" / slug
    quest_dir.mkdir(parents=True)
    (quest_dir / "change.json").write_text(json.dumps(
        {"id": slug, "title": "demo", "base_branch": "main",
         "layout_version": LAYOUT_VERSION}))
    long_line = "X" * 200  # no spaces: only wraps if the wrap width forces it
    (quest_dir / "events.jsonl").write_text(
        json.dumps({"id": "e1", "agent_id": None,
                    "timestamp": "2026-06-25T10:00:00+00:00",
                    "type": "step_started",
                    "payload": {"step": "s", "kind": "agent", "phase": "p",
                                "model": "m", "provider": "anthropic"}}) + "\n"
        + json.dumps({"id": "e2", "agent_id": "step:p/s",
                      "timestamp": "2026-06-25T10:00:01+00:00",
                      "type": "llm_response",
                      "payload": {"provider": "anthropic", "model": "m",
                                  "stop_reason": "end_turn", "usage": {},
                                  "raw": {"content": [{"type": "text",
                                          "text": long_line + "\n\n---\n\nafter"}]}}}) + "\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    # Wrap width huge (no prose wrap); physical/display width small (HR caps there).
    monkeypatch.setattr(m, "_term_width", lambda: 10000)
    monkeypatch.setattr(m, "_display_width", lambda: 40)
    monkeypatch.delenv("NO_COLOR", raising=False)

    rc = main(["log", slug])
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(long_line in ln for ln in lines)        # prose unwrapped (wrap width)
    assert any(ln.count("─") == 40 for ln in lines)    # HR capped to the display width
    assert not any(ln.count("─") > 40 for ln in lines)
