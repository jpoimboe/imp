"""Slash command dispatcher."""

from __future__ import annotations

from imp.cli.slash import is_slash_command, run_slash_command


def test_is_slash_command_recognizes_slash_prefix() -> None:
    assert is_slash_command("/help")
    assert is_slash_command("/  help")
    assert not is_slash_command("hello")
    assert not is_slash_command("")


def test_help_lists_commands() -> None:
    out: list[str] = []
    run_slash_command("/help", output_fn=out.append)
    text = "\n".join(out)
    assert "/help" in text
    assert "ESC" in text
    assert "Ctrl+C" in text or "Ctrl-C" in text


def test_unknown_command_reports() -> None:
    out: list[str] = []
    run_slash_command("/notreal", output_fn=out.append)
    text = "\n".join(out)
    assert "unknown" in text.lower()
    assert "notreal" in text
