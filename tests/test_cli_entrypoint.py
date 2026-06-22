from __future__ import annotations

import subprocess
import sys

from imp import __version__
from imp.cli.main import _OVERVIEW_COMMANDS, _build_parser, _command_names, main, render_overview


def test_overview_lists_each_common_command_and_footer() -> None:
    text = render_overview(color=False)
    for name in ("chat", "new", "resume", "ls", "log", "abandon", "config"):
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


def test_unknown_command_friendly_rc2(capsys) -> None:
    rc = main(["frobnicate"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "imp: unknown command 'frobnicate'. Run 'imp help'." in err
    # Must NOT fall through to argparse's raw invalid-choice list.
    assert "invalid choice" not in err.lower()


def test_known_command_still_dispatches(tmp_path, monkeypatch) -> None:
    # A real command is not swallowed by the unknown-command guard.
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".imp").mkdir()
    rc = main(["status"])
    assert rc == 0


def test_overview_commands_are_real_subcommands() -> None:
    """Drift guard: every name in the curated overview must be a live
    subcommand. Catches a renamed/removed command leaving a stale overview
    entry. The overview is intentionally a SUBSET (it omits e.g. `eval`,
    `init`), so this is a subset check, not equality."""
    real = _command_names(_build_parser())
    overview = {name for name, _ in _OVERVIEW_COMMANDS}
    stale = overview - real
    assert not stale, f"overview lists non-existent command(s): {sorted(stale)}"


def test_overview_new_summary_reads_as_opening_message() -> None:
    summary = dict(_OVERVIEW_COMMANDS)["new"]
    assert "opening message" in summary
    assert "title" not in summary


def test_main_swallows_broken_pipe_on_overview(monkeypatch) -> None:
    """The bare-`imp` overview path prints before the command dispatch, so it
    must also sit inside main()'s BrokenPipeError guard: a reader closing the
    pipe early returns the conventional 128+SIGPIPE status instead of escaping
    as an uncatchable traceback. os.open/os.dup2 are stubbed so the handler's
    stdout-fd redirect can't clobber pytest's own stdout."""
    import signal

    import imp.cli.main as m

    def boom(_color: bool) -> str:
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(m, "render_overview", boom)
    monkeypatch.setattr(m.os, "open", lambda *a, **k: 0)
    monkeypatch.setattr(m.os, "dup2", lambda *a, **k: None)

    rc = main([])
    assert rc == 128 + signal.SIGPIPE
