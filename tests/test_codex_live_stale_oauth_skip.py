"""Non-live unit test for the stale-OAuth skip helper in tests/_live.py.

Not `live`-marked: it runs in the ordinary (non-live) suite. The two current-shape
cases build their event by driving the real codex path and taking the emitted
payload — a hand-written event could pin a combination the code never produces."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imp.providers.codex_token_refresh import FAILED_INVALID_GRANT
from tests._live import (
    STALE_CODEX_OAUTH_SKIP_REASON,
    stale_codex_oauth_skip_reason,
)
from tests.test_codex_auth_terminality import (
    REUSE_MESSAGE,
    completing_turn,
    failing_turn,
    run_codex_turn,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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


@pytest.mark.anyio
async def test_the_mid_run_upgraded_event_still_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe replaced the vendor's prose with imp's marker under a new
    category — both halves of the old match are gone."""
    ran = await run_codex_turn(
        tmp_path, monkeypatch, turn=failing_turn({"message": REUSE_MESSAGE}),
        probe=FAILED_INVALID_GRANT)

    assert stale_codex_oauth_skip_reason(ran.events) == STALE_CODEX_OAUTH_SKIP_REASON


@pytest.mark.anyio
async def test_the_pre_spawn_dead_credential_event_still_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On this path no provider text ever existed: imp's marker is all there is."""
    ran = await run_codex_turn(
        tmp_path, monkeypatch, turn=completing_turn(), pre=FAILED_INVALID_GRANT)

    assert stale_codex_oauth_skip_reason(ran.events) == STALE_CODEX_OAUTH_SKIP_REASON


@pytest.mark.parametrize("category", ["auth", "auth_terminal"])
def test_a_genuine_401_never_skips_under_either_category(category: str) -> None:
    events: list[dict[str, Any]] = [{
        "type": "error",
        "payload": {"category": category, "message": "codex: unauthorized (401)"},
    }]
    assert stale_codex_oauth_skip_reason(events) is None
