# tests/test_telegram_selfcheck.py
"""Startup self-check + loud failure reporting for the Telegram driver.

The driver swallows transient send/poll errors (best-effort), but a *structural*
failure — a rejected bot token or an unreachable chat — would otherwise be
invisible (DEBUG only). The startup self-check validates the token (getMe) and
the chat (getChat) once and emits a WARNING-tagged, actionable diagnostic so the
host promotes it to a visible log. A persistent send failure is reported once
(edge-triggered), then suppressed until a send succeeds again."""
from __future__ import annotations

import pytest

from imp.remote.drivers import telegram as t


def test_log_warning_has_level_prefix(capsys: pytest.CaptureFixture[str]) -> None:
    t._log("boom", level="warning")
    err = capsys.readouterr().err
    assert err.startswith("WARNING: imp-telegram: boom")


def test_log_default_has_no_level_prefix(capsys: pytest.CaptureFixture[str]) -> None:
    t._log("hi")
    err = capsys.readouterr().err
    assert err.startswith("imp-telegram: hi")
    assert "WARNING" not in err


def test_self_check_warns_on_rejected_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def probe(token: str, method: str, params: dict) -> dict:
        assert method == "getMe"
        return {"ok": False, "error_code": 404, "description": "Not Found"}

    t.self_check("TOK", 5, probe=probe)
    err = capsys.readouterr().err
    assert err.startswith("WARNING:")
    assert "IMP_TELEGRAM_TOKEN" in err


def test_self_check_warns_on_unreachable_chat(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def probe(token: str, method: str, params: dict) -> dict:
        if method == "getMe":
            return {"ok": True, "result": {"username": "bot"}}
        assert method == "getChat"
        assert params == {"chat_id": 5}
        return {"ok": False, "error_code": 400, "description": "chat not found"}

    t.self_check("TOK", 5, probe=probe)
    err = capsys.readouterr().err
    assert err.startswith("WARNING:")
    assert "/start" in err


def test_self_check_silent_when_healthy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    t.self_check("TOK", 5, probe=lambda *a, **k: {"ok": True, "result": {}})
    assert capsys.readouterr().err == ""


def test_self_check_stays_quiet_on_transient_network_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A network blip is not a config error: don't cry wolf at WARNING — the
    # poll/send loops will retry. (A DEBUG line is fine; no WARNING tag.)
    def probe(token: str, method: str, params: dict) -> dict:
        raise OSError("connection refused")

    t.self_check("TOK", 5, probe=probe)
    assert "WARNING" not in capsys.readouterr().err


def test_send_gate_warns_once_then_suppresses_until_success(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gate = t._SendGate()
    gate.fail(RuntimeError("x"))
    assert capsys.readouterr().err.startswith("WARNING:")  # first failure is loud
    gate.fail(RuntimeError("y"))
    assert "WARNING" not in capsys.readouterr().err        # repeat suppressed
    gate.ok()                                              # a success re-arms
    gate.fail(RuntimeError("z"))
    assert capsys.readouterr().err.startswith("WARNING:")
