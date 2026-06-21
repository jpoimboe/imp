"""Tests for the imp log pager (src/imp/cli/main.py::_page_or_print).

Each test patches `subprocess.Popen` at the import site
(`imp.cli.main.subprocess.Popen`) so no real pager is spawned. The helper
takes `lines: Iterable[str]` (no trailing newlines) and writes one line per
iteration, appending "\n" itself — both in the pager-spawn path (via
`proc.stdin.write`) and in the inline fallback (via `print()`).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from imp.engine import backlog
from imp.engine.change import LAYOUT_VERSION


def _fake_tty_stdout(monkeypatch: pytest.MonkeyPatch, *, isatty: bool) -> None:
    # Lie about tty without replacing sys.stdout (capsys must keep its handle).
    import sys
    monkeypatch.setattr(sys.stdout, "isatty", lambda: isatty)


def test_no_pager_writes_inline(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    with patch("imp.cli.main.subprocess.Popen") as popen:
        _page_or_print(iter(["alpha", "beta"]), no_pager=True)
    assert popen.call_count == 0
    assert capsys.readouterr().out == "alpha\nbeta\n"


def test_non_tty_writes_inline(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=False)
    with patch("imp.cli.main.subprocess.Popen") as popen:
        _page_or_print(iter(["alpha", "beta"]), no_pager=False)
    assert popen.call_count == 0
    assert capsys.readouterr().out == "alpha\nbeta\n"


def test_pager_empty_string_disables_pager(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.setenv("PAGER", "")
    with patch("imp.cli.main.subprocess.Popen") as popen:
        _page_or_print(iter(["alpha"]), no_pager=False)
    assert popen.call_count == 0
    assert capsys.readouterr().out == "alpha\n"


def test_pager_whitespace_disables_pager(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.setenv("PAGER", "   ")
    with patch("imp.cli.main.subprocess.Popen") as popen:
        _page_or_print(iter(["alpha"]), no_pager=False)
    assert popen.call_count == 0
    assert capsys.readouterr().out == "alpha\n"


def test_pager_unparseable_falls_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """`shlex.split` raises `ValueError` on malformed quoting like
    `less "unclosed`. The helper must catch it and degrade to inline print
    rather than crashing `imp log`."""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.setenv("PAGER", 'less "unclosed')
    with patch("imp.cli.main.subprocess.Popen") as popen:
        _page_or_print(iter(["alpha"]), no_pager=False)
    assert popen.call_count == 0
    assert capsys.readouterr().out == "alpha\n"


def test_tty_spawns_default_pager(monkeypatch: pytest.MonkeyPatch) -> None:
    """TTY + no_pager=False + $PAGER unset → spawn `less -F -R -X`, writing
    each input line followed by a newline to the pager's stdin."""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.delenv("PAGER", raising=False)

    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.wait = MagicMock(return_value=0)

    with patch("imp.cli.main.subprocess.Popen", return_value=proc) as popen:
        _page_or_print(iter(["alpha", "beta"]), no_pager=False)

    assert popen.call_count == 1
    call_args, call_kwargs = popen.call_args
    assert call_args[0] == ["less", "-F", "-R", "-X"]
    assert call_kwargs.get("stdin") == subprocess.PIPE
    writes = [c.args[0] for c in proc.stdin.write.call_args_list]
    assert writes == ["alpha\n", "beta\n"]
    proc.stdin.close.assert_called_once()
    proc.wait.assert_called_once()


def test_color_ansi_passes_through_pager(monkeypatch: pytest.MonkeyPatch) -> None:
    """ANSI escapes in input lines reach the pager unmodified — `less -R`
    renders them. Guards against an accidental encode/strip in the write loop."""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.delenv("PAGER", raising=False)

    proc = MagicMock()
    proc.stdin = MagicMock()

    colored = "\x1b[32mgreen\x1b[0m"
    with patch("imp.cli.main.subprocess.Popen", return_value=proc):
        _page_or_print(iter([colored]), no_pager=False)

    written = "".join(c.args[0] for c in proc.stdin.write.call_args_list)
    assert written == "\x1b[32mgreen\x1b[0m\n"


def test_pager_env_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    """`PAGER="bat -p"` → Popen called with `["bat", "-p"]` (shlex split)."""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.setenv("PAGER", "bat -p")

    proc = MagicMock()
    proc.stdin = MagicMock()

    with patch("imp.cli.main.subprocess.Popen", return_value=proc) as popen:
        _page_or_print(iter(["alpha"]), no_pager=False)

    assert popen.call_args.args[0] == ["bat", "-p"]


def test_less_env_set_when_pager_is_less_and_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PAGER=less + no LESS in env → child env has LESS=FRX."""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.setenv("PAGER", "less")
    monkeypatch.delenv("LESS", raising=False)

    proc = MagicMock()
    proc.stdin = MagicMock()

    with patch("imp.cli.main.subprocess.Popen", return_value=proc) as popen:
        _page_or_print(iter(["alpha"]), no_pager=False)

    env = popen.call_args.kwargs.get("env")
    assert env is not None
    assert env.get("LESS") == "FRX"


def test_less_env_not_overridden_when_already_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PAGER=less + user-set LESS=SR → child env keeps LESS=SR."""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.setenv("PAGER", "less")
    monkeypatch.setenv("LESS", "SR")

    proc = MagicMock()
    proc.stdin = MagicMock()

    with patch("imp.cli.main.subprocess.Popen", return_value=proc) as popen:
        _page_or_print(iter(["alpha"]), no_pager=False)

    env = popen.call_args.kwargs.get("env")
    assert env is not None
    assert env.get("LESS") == "SR"


def test_less_env_set_for_default_pager_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PAGER unset (default `less -F -R -X`) + no LESS → child env has
    LESS=FRX, same treatment as PAGER=less."""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.delenv("PAGER", raising=False)
    monkeypatch.delenv("LESS", raising=False)

    proc = MagicMock()
    proc.stdin = MagicMock()

    with patch("imp.cli.main.subprocess.Popen", return_value=proc) as popen:
        _page_or_print(iter(["alpha"]), no_pager=False)

    env = popen.call_args.kwargs.get("env")
    assert env is not None
    assert env.get("LESS") == "FRX"


def test_less_env_not_set_for_non_less_pager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PAGER=bat → no LESS injection (we don't touch unrelated env keys)."""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.setenv("PAGER", "bat -p")
    monkeypatch.delenv("LESS", raising=False)

    proc = MagicMock()
    proc.stdin = MagicMock()

    with patch("imp.cli.main.subprocess.Popen", return_value=proc) as popen:
        _page_or_print(iter(["alpha"]), no_pager=False)

    env = popen.call_args.kwargs.get("env")
    assert env is None


def test_broken_pipe_on_write_swallowed_and_child_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the pager exited (user pressed `q`), `proc.stdin.write` raises
    BrokenPipeError. The helper swallows it, then still closes stdin and waits."""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.delenv("PAGER", raising=False)

    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.write.side_effect = BrokenPipeError("pager gone")
    proc.wait = MagicMock(return_value=0)

    with patch("imp.cli.main.subprocess.Popen", return_value=proc):
        _page_or_print(iter(["alpha", "beta"]), no_pager=False)

    proc.stdin.close.assert_called_once()
    proc.wait.assert_called_once()


def test_broken_pipe_on_close_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`proc.stdin.close()` can itself raise BrokenPipeError if a buffered
    write hasn't flushed and the pager is gone. Suppressed; `wait()` still runs."""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.delenv("PAGER", raising=False)

    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.close.side_effect = BrokenPipeError("pager gone before flush")
    proc.wait = MagicMock(return_value=0)

    with patch("imp.cli.main.subprocess.Popen", return_value=proc):
        _page_or_print(iter(["alpha"]), no_pager=False)

    proc.wait.assert_called_once()


def test_other_write_exception_cleans_up_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-BrokenPipe exception during writes propagates, but cleanup
    (`close()` + `wait()`) still runs via the `finally` block."""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.delenv("PAGER", raising=False)

    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.write.side_effect = RuntimeError("boom")
    proc.wait = MagicMock(return_value=0)

    with (
        patch("imp.cli.main.subprocess.Popen", return_value=proc),
        pytest.raises(RuntimeError, match="boom"),
    ):
        _page_or_print(iter(["alpha"]), no_pager=False)

    proc.stdin.close.assert_called_once()
    proc.wait.assert_called_once()


def test_missing_pager_falls_back_to_print(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """`Popen` raising FileNotFoundError (binary missing) → silent inline print."""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.setenv("PAGER", "nope-not-a-real-pager-xyz")

    with patch(
        "imp.cli.main.subprocess.Popen",
        side_effect=FileNotFoundError(2, "No such file", "nope-not-a-real-pager-xyz"),
    ):
        _page_or_print(iter(["alpha", "beta"]), no_pager=False)

    assert capsys.readouterr().out == "alpha\nbeta\n"


def test_missing_pager_oserror_falls_back_to_print(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """`Popen` raising a generic OSError (e.g. PermissionError) also degrades
    to inline print."""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.setenv("PAGER", "/etc/passwd")

    with patch(
        "imp.cli.main.subprocess.Popen",
        side_effect=PermissionError(13, "Permission denied"),
    ):
        _page_or_print(iter(["alpha"]), no_pager=False)

    assert capsys.readouterr().out == "alpha\n"


def test_keyboard_interrupt_in_wait_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C in the pager interrupts `proc.wait()` with KeyboardInterrupt.
    The helper catches it and re-waits until the child exits, so no traceback
    leaks while the pager is still up."""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.delenv("PAGER", raising=False)

    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.wait.side_effect = [KeyboardInterrupt, 0]

    with patch("imp.cli.main.subprocess.Popen", return_value=proc):
        _page_or_print(iter(["alpha"]), no_pager=False)

    assert proc.wait.call_count == 2


def _make_change_worktree(control: Path, slug: str, title: str) -> Path:
    """Create a minimal on-disk Change home under control/.imp/changes/<slug>
    with one rendered step (phase "spec") so the narrative reaches output."""
    change_dir = control / ".imp" / "changes" / slug
    change_dir.mkdir(parents=True)
    (change_dir / "change.json").write_text(
        json.dumps({"id": slug, "title": title, "base_branch": "main",
                    "layout_version": LAYOUT_VERSION,
                    "created_at": "2026-05-27T10:00:00+00:00"}),
    )
    state_dir = change_dir / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({"step": "-", "status": "paused"}),
    )
    (change_dir / "events.jsonl").write_text(
        json.dumps({
            "timestamp": "2026-05-27T10:00:00+00:00",
            "type": "change_created",
            "payload": {"title": title},
        }) + "\n"
        + json.dumps({
            "id": "e2", "agent_id": None,
            "timestamp": "2026-05-27T10:00:01+00:00",
            "type": "step_started",
            "payload": {"step": "s", "kind": "agent", "phase": "spec",
                        "model": "m", "provider": "anthropic"},
        }) + "\n",
    )
    return change_dir


def test_main_log_invokes_pager_on_tty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main(["log", "<slug>"])` on a TTY routes the narrative through
    _page_or_print: Popen called once, nothing leaked to capsys, rendered
    phase reached the pager's stdin, cleanup ran."""
    import imp.cli.main as m

    slug = "my-smoke"
    _make_change_worktree(tmp_path, slug, "smoke")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("PAGER", raising=False)
    _fake_tty_stdout(monkeypatch, isatty=True)

    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.wait = MagicMock(return_value=0)

    with patch("imp.cli.main.subprocess.Popen", return_value=proc) as popen:
        rc = m.main(["log", slug])

    assert rc == 0
    assert popen.call_count == 1, "_cmd_log must route TTY output through _page_or_print"
    assert capsys.readouterr().out == ""
    writes = "".join(c.args[0] for c in proc.stdin.write.call_args_list)
    assert "spec" in writes
    proc.stdin.close.assert_called_once()
    proc.wait.assert_called_once()


def test_main_log_no_pager_flag_skips_pager_on_tty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main(["log", "--no-pager", "<slug>"])` on a TTY → no Popen, inline
    print. Catches a refactor that drops `args.no_pager` from the helper call."""
    import imp.cli.main as m

    slug = "my-smoke"
    _make_change_worktree(tmp_path, slug, "smoke")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("PAGER", raising=False)
    _fake_tty_stdout(monkeypatch, isatty=True)

    with patch("imp.cli.main.subprocess.Popen") as popen:
        rc = m.main(["log", "--no-pager", slug])

    assert rc == 0
    assert popen.call_count == 0
    assert "spec" in capsys.readouterr().out


def test_main_log_non_tty_prints_inline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Not a TTY (pipe/redirect) → no Popen, inline print, as today."""
    import imp.cli.main as m

    slug = "my-smoke"
    _make_change_worktree(tmp_path, slug, "smoke")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("PAGER", raising=False)
    _fake_tty_stdout(monkeypatch, isatty=False)

    with patch("imp.cli.main.subprocess.Popen") as popen:
        rc = m.main(["log", slug])

    assert rc == 0
    assert popen.call_count == 0
    assert "spec" in capsys.readouterr().out


def test_main_log_summary_routes_through_pager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main(["log", "-s", "<slug>"])` on a TTY routes the summary view
    through _page_or_print (Popen once, cleanup ran)."""
    import imp.cli.main as m

    slug = "my-smoke"
    _make_change_worktree(tmp_path, slug, "smoke")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("PAGER", raising=False)
    _fake_tty_stdout(monkeypatch, isatty=True)

    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.wait = MagicMock(return_value=0)

    with patch("imp.cli.main.subprocess.Popen", return_value=proc) as popen:
        rc = m.main(["log", "-s", slug])

    assert rc == 0
    assert popen.call_count == 1, "--summary must route through _page_or_print"
    assert capsys.readouterr().out == ""
    proc.stdin.close.assert_called_once()
    proc.wait.assert_called_once()


def test_main_log_backlog_routes_through_pager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A backlog-only Change (no events) renders its card through
    _page_or_print on a TTY (Popen once, cleanup ran)."""
    import imp.cli.main as m

    slug = "backlog-only"
    backlog.write_record(
        tmp_path, change_id=slug, title="queued work",
        description="", depends_on=[], created_by="user",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("PAGER", raising=False)
    _fake_tty_stdout(monkeypatch, isatty=True)

    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.wait = MagicMock(return_value=0)

    with patch("imp.cli.main.subprocess.Popen", return_value=proc) as popen:
        rc = m.main(["log", slug])

    assert rc == 0
    assert popen.call_count == 1, "backlog card must route through _page_or_print"
    assert capsys.readouterr().out == ""
    proc.stdin.close.assert_called_once()
    proc.wait.assert_called_once()
