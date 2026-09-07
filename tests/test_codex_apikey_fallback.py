"""Scope 4: a dead ChatGPT credential falls back to OPENAI_API_KEY at spawn."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from imp.cli.accounting import is_metered
from imp.providers import codex_runner
from imp.providers.codex_config import seed_auth
from imp.providers.codex_token_refresh import FAILED_INVALID_GRANT
from tests.codex_fakes import (
    agent_message,
    mcp_call,
    thread_started,
    turn_completed,
)
from tests.test_codex_auth_terminality import error_payload, run_codex_turn

API_KEY = "sk-test-fallback"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class SeedSpy:
    """Records what seed_auth was asked to write — teardown shreds the file
    before the test can read it back."""

    def __init__(self) -> None:
        self.modes: list[str] = []
        self.written: list[dict[str, Any]] = []

    def __call__(self, *, mode: str, canonical_path: Path, codex_home: Path,
                 env: Any) -> Path:
        target = seed_auth(mode=mode, canonical_path=canonical_path,
                           codex_home=codex_home, env=env)
        self.modes.append(mode)
        self.written.append(json.loads(target.read_text()))
        return target


def talking_turn() -> list[Any]:
    """A turn with prose, so the flipped auth mode is observable on a real
    agent_message rather than only on turn.completed's empty carrier."""
    return [thread_started("thr-1"),
            agent_message("ok"),
            mcp_call(name="complete_step", args={"answer": "42"},
                     result="step complete", item_id="cs-1"),
            turn_completed(input_tokens=1, output_tokens=1)]


@pytest.mark.anyio
async def test_a_dead_credential_falls_back_to_the_api_key_at_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No exception, and CODEX_HOME is seeded from the key — not from the dead
    OAuth pair the canonical file still declares."""
    spy = SeedSpy()
    monkeypatch.setattr(codex_runner, "seed_auth", spy)

    ran = await run_codex_turn(
        tmp_path, monkeypatch, turn=talking_turn(),
        pre=FAILED_INVALID_GRANT, api_key=API_KEY)

    assert [e.get("type") for e in ran.events].count("agent_result") == 1
    assert [e for e in ran.events if e.get("type") == "error"] == []
    assert spy.modes == ["apikey"]
    assert spy.written == [{"auth_mode": "apikey", "OPENAI_API_KEY": API_KEY}]


@pytest.mark.anyio
async def test_the_billing_switch_is_journaled_as_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moving a subscription run onto metered API billing is never silent."""
    ran = await run_codex_turn(
        tmp_path, monkeypatch, turn=talking_turn(),
        pre=FAILED_INVALID_GRANT, api_key=API_KEY)

    warnings = [e["payload"] for e in ran.elog.read() if e["type"] == "warning"]
    assert len(warnings) == 1, warnings
    assert warnings[0]["message"] == (
        "codex: ChatGPT credential is dead; continuing with OPENAI_API_KEY — "
        "this bills the OpenAI API, not your ChatGPT subscription")


@pytest.mark.anyio
async def test_the_fallback_run_records_as_metered_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """proc_state["auth_mode"] is what every agent_message carries; a run that
    genuinely billed the OpenAI API must not report as notional spend."""
    ran = await run_codex_turn(
        tmp_path, monkeypatch, turn=talking_turn(),
        pre=FAILED_INVALID_GRANT, api_key=API_KEY)

    modes = [e["payload"].get("auth_mode") for e in ran.elog.read()
             if e["type"] == "agent_message"]
    assert modes and set(modes) == {"apikey"}
    assert all(is_metered("codex", mode) for mode in modes)


@pytest.mark.anyio
async def test_a_dead_credential_with_no_api_key_still_fails_terminally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The escape hatch exists only when there is a key to escape to."""
    ran = await run_codex_turn(
        tmp_path, monkeypatch, turn=talking_turn(), pre=FAILED_INVALID_GRANT)
    payload = error_payload(ran)

    assert payload["category"] == "auth_terminal"
    assert payload["message"] == (
        "codex: ChatGPT OAuth refresh token is dead; run `codex login`. Sharing "
        "~/.codex/auth.json across machines kills it — each refresh rotates a "
        "single-use token.")
