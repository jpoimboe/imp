from __future__ import annotations

import imp.cli.main as m


def _remote_control_parser():
    parser = m._build_parser()
    for action in parser._actions:
        if hasattr(action, "choices") and action.choices and "remote-control" in action.choices:
            return action.choices["remote-control"]
    raise AssertionError("remote-control subparser not found")


def test_remote_control_help_has_description_paragraph() -> None:
    # Normalize whitespace: argparse hard-wraps the description to terminal width
    # (80 cols with no tty in CI), inserting newlines mid-sentence. Collapsing all
    # whitespace to single spaces makes the substring match width-independent.
    help_text = " ".join(_remote_control_parser().format_help().split())
    assert "relays every quest's activity to a single chat transport" in help_text
    assert "start launches it; stop shuts it down." in help_text
