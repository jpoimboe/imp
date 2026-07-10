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
    assert "Start a new Quest" in out
    assert "Resume a paused" in out
    assert "List your changes" in out
    assert "one change's detail" in out
    assert "effective config" in out


def test_new_help_documents_the_message_positional(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _help_for(["new", "-h"], capsys)
    assert "MESSAGE" in out
    assert "opening intent" in out.lower()
    assert "front-door" not in out.lower()
    assert "threshold" not in out.lower()


def test_resume_help_documents_the_token_positional(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _help_for(["resume", "-h"], capsys)
    assert "Quest id or resume token" in out


def test_log_help_documents_the_token_positional(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _help_for(["log", "-h"], capsys)
    assert "Quest id or its short token" in out


def test_help_lists_model_command() -> None:
    from imp.cli.slash import _help
    out: list[str] = []
    _help(out.append)
    assert any(line.strip().startswith("/model") for line in out)


def test_opening_hint_mentions_model() -> None:
    from imp.cli.main import OPENING_HINT
    assert "/model" in OPENING_HINT
