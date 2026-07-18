"""`_emit_cli_error`: the single `imp: <message>` stderr sink for CLI usage errors."""
from __future__ import annotations

import pytest

from imp.cli import main as cli_main


def test_emit_cli_error_prefixes_and_targets_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_main._emit_cli_error("bad token 'x'")
    captured = capsys.readouterr()
    assert captured.err == "imp: bad token 'x'\n"
    assert captured.out == ""
