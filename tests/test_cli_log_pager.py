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
