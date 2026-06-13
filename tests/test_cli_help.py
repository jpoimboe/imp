from __future__ import annotations

import pytest

from imp.cli.main import _build_parser


def _help_for(argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(argv)
    return capsys.readouterr().out


def test_top_level_help_describes_every_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _help_for(["-h"], capsys)
    assert "Start a new Change" in out
    assert "Resume a paused" in out
    assert "List in-flight Changes" in out
    assert "effective config" in out


def test_new_help_documents_the_message_positional(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _help_for(["new", "-h"], capsys)
    assert "MESSAGE" in out
    assert "opening intent" in out.lower()


def test_resume_help_documents_the_token_positional(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _help_for(["resume", "-h"], capsys)
    assert "Change id or resume token" in out
