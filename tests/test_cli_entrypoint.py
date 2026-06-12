from __future__ import annotations

import subprocess
import sys

from imp import __version__
from imp.cli.main import main, render_overview


def test_overview_lists_each_common_command_and_footer() -> None:
    text = render_overview(color=False)
    for name in ("new", "resume", "status", "log", "abandon", "config"):
        assert name in text
    # Getting-started pointer and the per-command detail footer.
    assert "imp init" in text
    assert "Run 'imp <command> -h' for details." in text


def test_overview_resume_line_is_accurate_and_unframed() -> None:
    text = render_overview(color=False)
    # Accuracy: resume must NOT claim it resumes "the most recent" change.
    assert "most recent" not in text.lower()
    # Content, not an argparse error: no usage:/error: framing words.
    assert "usage" not in text.lower()
    assert "error" not in text.lower()


def test_bare_imp_prints_overview_rc0(capsys) -> None:
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Common commands:" in out
    assert "Run 'imp <command> -h' for details." in out
    # No argparse usage/error framing on the front door.
    assert "usage:" not in out.lower()
    assert "error:" not in out.lower()


def test_version_flag_prints_version_rc0() -> None:
    r = subprocess.run(
        [sys.executable, "-m", "imp", "--version"],
        capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == f"imp {__version__}"


def test_imp_help_prints_overview_rc0(capsys) -> None:
    rc = main(["help"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Common commands:" in out
    assert "Run 'imp <command> -h' for details." in out
    assert "usage:" not in out.lower()
    assert "error:" not in out.lower()


def test_imp_help_command_equals_command_dash_h() -> None:
    helped = subprocess.run(
        [sys.executable, "-m", "imp", "help", "resume"],
        capture_output=True, text=True, check=False,
    )
    direct = subprocess.run(
        [sys.executable, "-m", "imp", "resume", "-h"],
        capture_output=True, text=True, check=False,
    )
    assert helped.returncode == 0
    assert direct.returncode == 0
    assert helped.stdout == direct.stdout


def test_imp_help_unknown_command_rc2(capsys) -> None:
    rc = main(["help", "frobnicate"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown command 'frobnicate'" in err
    assert "imp help" in err
