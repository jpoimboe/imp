"""Non-live unit test for the stale-OAuth skip helper in tests/_live.py.

Not `live`-marked: it runs in the ordinary (non-live) suite and exercises the
events->reason-or-None logic against constructed event dicts, so it never
triggers live gating."""

from __future__ import annotations

from tests._live import (
    STALE_CODEX_OAUTH_SKIP_REASON,
    stale_codex_oauth_skip_reason,
)


def test_returns_reason_on_stale_oauth_auth_error() -> None:
    events = [
        {"type": "agent_result", "payload": {"word": "banana"}},
        {
            "type": "error",
            "payload": {
                "category": "auth",
                "message": "codex: refresh token was already used. "
                           "Please log out and sign in again.",
            },
        },
    ]
    assert stale_codex_oauth_skip_reason(events) == STALE_CODEX_OAUTH_SKIP_REASON


def test_returns_reason_on_already_used_signature_alone() -> None:
    events = [
        {
            "type": "error",
            "payload": {
                "category": "auth",
                "message": "codex: refresh token already used.",
            },
        },
    ]
    assert stale_codex_oauth_skip_reason(events) == STALE_CODEX_OAUTH_SKIP_REASON


def test_returns_none_on_benign_events() -> None:
    # An agent_result plus a genuine 401 auth error (auth category, but NOT the
    # refresh-reuse signature) must NOT skip.
    events = [
        {"type": "agent_result", "payload": {"word": "banana"}},
        {
            "type": "error",
            "payload": {
                "category": "auth",
                "message": "codex: unauthorized (401)",
            },
        },
    ]
    assert stale_codex_oauth_skip_reason(events) is None
