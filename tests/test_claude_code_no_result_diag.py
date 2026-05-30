"""Fix J: no_result_message enriches detail with subprocess stderr/exit."""

from __future__ import annotations

from imp.providers.claude_code import _subprocess_diag


class _FakeTransportClient:
    """Shape a fake SDK client exposing a transport with a process the runner
    can probe for exit/stderr."""
    class _Transport:
        returncode = 3
        stderr_tail = "Traceback: boom"
    _transport = _Transport()


def test_subprocess_diag_extracts_exit_and_stderr() -> None:
    diag = _subprocess_diag(_FakeTransportClient())
    assert diag.get("exit") == 3
    assert "boom" in str(diag.get("stderr_tail", ""))


def test_subprocess_diag_is_empty_when_unavailable() -> None:
    diag = _subprocess_diag(object())
    assert diag == {}
