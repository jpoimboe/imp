"""Tests for the imp log pager (src/imp/cli/main.py::_page_or_print).

Each test patches `subprocess.Popen` at the import site
(`imp.cli.main.subprocess.Popen`) so no real pager is spawned.

The helper takes `lines: Iterable[str]` (no trailing newlines) and is
expected to write one line per iteration, appending "\n" itself — both
in the pager-spawn path (via `proc.stdin.write`) and in the fallback
print path (via `print()` which adds its own newline).
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from imp.cli.main import main


def test_no_pager_flag_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """`imp log --no-pager` parses without error and exits 0 against a
    minimal Change. This locks in the argparse wiring before _page_or_print
    exists."""
    cdir = tmp_path / ".imp" / "changes" / "1"
    cdir.mkdir(parents=True)
    (cdir / "change.json").write_text(json.dumps({"id": 1, "title": "smoke"}))
    (cdir / "events.jsonl").write_text(
        json.dumps({
            "timestamp": "2026-05-27T10:00:00+00:00",
            "type": "change_created",
            "payload": {"change_id": 1, "title": "smoke"},
        }) + "\n",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)

    rc = main(["log", "--no-pager", "1"])
    assert rc == 0
    assert "change_created" in capsys.readouterr().out


def _fake_tty_stdout(monkeypatch: pytest.MonkeyPatch, *, isatty: bool) -> None:
    """Patch `sys.stdout.isatty` (the bound method) to return a fixed bool.

    We can't replace `sys.stdout` wholesale because `capsys` captures it; we
    only want to lie about the TTY check.

    Assumes pytest capture is on (the default). If these tests are ever run
    with `-s` (capture disabled), `sys.stdout` is the real terminal stream
    and the `setattr` may not stick. CI runs with capture on, so this is
    fine in practice.
    """
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
    out = capsys.readouterr().out
    assert out == "alpha\nbeta\n"


def test_non_tty_writes_inline(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=False)
    with patch("imp.cli.main.subprocess.Popen") as popen:
        _page_or_print(iter(["alpha", "beta"]), no_pager=False)
    assert popen.call_count == 0
    out = capsys.readouterr().out
    assert out == "alpha\nbeta\n"


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
    """`shlex.split` raises `ValueError` on a malformed quoted value like
    `less "unclosed`. The helper must catch it and degrade to inline
    print rather than crashing the whole `imp log` invocation."""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.setenv("PAGER", 'less "unclosed')
    with patch("imp.cli.main.subprocess.Popen") as popen:
        _page_or_print(iter(["alpha"]), no_pager=False)
    assert popen.call_count == 0
    assert capsys.readouterr().out == "alpha\n"


def test_tty_spawns_default_pager(monkeypatch: pytest.MonkeyPatch) -> None:
    """When stdout is a TTY, no_pager=False, and $PAGER is unset, the
    helper spawns `less -F -R -X` and writes each input line followed by
    a newline to the pager's stdin."""
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
    # Lines were written, each with trailing newline.
    writes = [c.args[0] for c in proc.stdin.write.call_args_list]
    assert writes == ["alpha\n", "beta\n"]
    # Cleanup ran.
    proc.stdin.close.assert_called_once()
    proc.wait.assert_called_once()


def test_color_ansi_passes_through_pager(monkeypatch: pytest.MonkeyPatch) -> None:
    """ANSI escape sequences in input lines reach the pager unmodified —
    `less -R` is responsible for rendering them. This guards against any
    accidental `.encode()`/strip step in the write loop."""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.delenv("PAGER", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)

    proc = MagicMock()
    proc.stdin = MagicMock()

    colored = "\x1b[32mgreen\x1b[0m"
    with patch("imp.cli.main.subprocess.Popen", return_value=proc):
        _page_or_print(iter([colored]), no_pager=False)

    written = "".join(c.args[0] for c in proc.stdin.write.call_args_list)
    assert "\x1b[" in written
    assert "\x1b[32mgreen\x1b[0m\n" == written


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
    """PAGER=less + user set LESS=SR → child env keeps LESS=SR."""
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
    """PAGER unset (default `less -F -R -X`) + no LESS in env → child env
    has LESS=FRX. The default pager is `less`, so it should get the same
    treatment as PAGER=less."""
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
    """PAGER=bat → no LESS injection. (We don't touch unrelated env keys.)"""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.setenv("PAGER", "bat -p")
    monkeypatch.delenv("LESS", raising=False)

    proc = MagicMock()
    proc.stdin = MagicMock()

    with patch("imp.cli.main.subprocess.Popen", return_value=proc) as popen:
        _page_or_print(iter(["alpha"]), no_pager=False)

    # Either env is None (we didn't bother passing it) or env contains
    # no LESS key. Both are acceptable; the contract is "don't inject."
    env = popen.call_args.kwargs.get("env")
    if env is not None:
        assert "LESS" not in env


def test_broken_pipe_on_write_swallowed_and_child_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the pager exits (e.g. user pressed `q`), `proc.stdin.write` raises
    BrokenPipeError. The helper must swallow it, then still close stdin and
    wait() the child."""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.delenv("PAGER", raising=False)

    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.write.side_effect = BrokenPipeError("pager gone")
    proc.wait = MagicMock(return_value=0)

    with patch("imp.cli.main.subprocess.Popen", return_value=proc):
        # Must not raise.
        _page_or_print(iter(["alpha", "beta"]), no_pager=False)

    proc.stdin.close.assert_called_once()
    proc.wait.assert_called_once()


def test_broken_pipe_on_close_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`proc.stdin.close()` can itself raise BrokenPipeError if the buffered
    write hasn't flushed yet and the pager is gone. Suppressed; `wait()`
    still runs."""
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
    """A non-BrokenPipe exception during writes must propagate, but
    cleanup (`close()` + `wait()`) must still run via the `finally` block."""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.delenv("PAGER", raising=False)

    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.write.side_effect = RuntimeError("boom")
    proc.wait = MagicMock(return_value=0)

    with patch("imp.cli.main.subprocess.Popen", return_value=proc):
        with pytest.raises(RuntimeError, match="boom"):
            _page_or_print(iter(["alpha"]), no_pager=False)

    proc.stdin.close.assert_called_once()
    proc.wait.assert_called_once()


def test_missing_pager_falls_back_to_print(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """If `Popen` raises FileNotFoundError (pager binary missing), the
    helper silently degrades to inline print. No warning, no exit."""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.setenv("PAGER", "nope-not-a-real-pager-xyz")

    with patch(
        "imp.cli.main.subprocess.Popen",
        side_effect=FileNotFoundError(2, "No such file", "nope-not-a-real-pager-xyz"),
    ):
        _page_or_print(iter(["alpha", "beta"]), no_pager=False)

    out = capsys.readouterr().out
    assert out == "alpha\nbeta\n"


def test_missing_pager_oserror_falls_back_to_print(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """`Popen` can also raise generic OSError (e.g. PermissionError, which
    is an OSError subclass). Also degrades to print()."""
    from imp.cli.main import _page_or_print

    _fake_tty_stdout(monkeypatch, isatty=True)
    monkeypatch.setenv("PAGER", "/etc/passwd")  # not executable

    with patch(
        "imp.cli.main.subprocess.Popen",
        side_effect=PermissionError(13, "Permission denied"),
    ):
        _page_or_print(iter(["alpha"]), no_pager=False)

    assert capsys.readouterr().out == "alpha\n"
