from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.state import InMemoryEventLog
from imp.providers import codex_runner
from imp.providers import codex_token_refresh as ctr
from imp.providers import errors as perr
from imp.providers.codex_runner import CodexAgentRunner
from tests.codex_fakes import FakeCodex
from tests.test_codex_runner_happy_path import (
    _build_invocation,
    _seed_auth,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_helper_journals_outcome_and_raises_on_dead_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _dead(now_ms: int, *, env: object = None) -> ctr.RefreshOutcome:
        return ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT)

    monkeypatch.setattr(codex_runner, "refresh_canonical_token", _dead)
    log = InMemoryEventLog()
    with pytest.raises(perr.AuthTerminalError):
        codex_runner._pre_spawn_token_refresh(log, "step:t")

    # Journaled BEFORE the raise; payload carries only the outcome string.
    events = [e for e in log.read() if e["type"] == "token_refresh"]
    assert len(events) == 1
    assert events[0]["agent_id"] == "step:t"
    assert events[0]["payload"] == {"outcome": "failed_invalid_grant"}


def test_helper_journals_expires_at_when_refreshed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _ok(now_ms: int, *, env: object = None) -> ctr.RefreshOutcome:
        return ctr.RefreshOutcome(ctr.REFRESHED, expires_at=1_700_000_000_000)

    monkeypatch.setattr(codex_runner, "refresh_canonical_token", _ok)
    log = InMemoryEventLog()
    codex_runner._pre_spawn_token_refresh(log, "step:t")  # must NOT raise
    events = [e for e in log.read() if e["type"] == "token_refresh"]
    assert events[0]["payload"] == {
        "outcome": "refreshed", "expires_at": 1_700_000_000_000}


@pytest.mark.parametrize(
    "outcome", [ctr.SKIPPED_FRESH, ctr.NOT_APPLICABLE, ctr.FAILED_TRANSIENT])
def test_helper_does_not_raise_on_non_dead_outcomes(
    monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    def _out(now_ms: int, *, env: object = None) -> ctr.RefreshOutcome:
        return ctr.RefreshOutcome(outcome)

    monkeypatch.setattr(codex_runner, "refresh_canonical_token", _out)
    log = InMemoryEventLog()
    codex_runner._pre_spawn_token_refresh(log, "step:t")  # must NOT raise
    events = [e for e in log.read() if e["type"] == "token_refresh"]
    assert events[0]["payload"] == {"outcome": outcome}


@pytest.mark.anyio
async def test_dead_token_short_circuits_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_auth(tmp_path)  # writes a chatgpt-mode auth.json
    canonical = tmp_path / "auth.json"

    def _dead(now_ms: int, *, env: object = None) -> ctr.RefreshOutcome:
        return ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT)

    monkeypatch.setattr(codex_runner, "refresh_canonical_token", _dead)

    fake = FakeCodex(scripted_turns=[[]])  # must never be started
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: fake)
    runner._canonical_auth_override = canonical

    elog = InMemoryEventLog()
    inv = _build_invocation(tmp_path, event_log=elog)
    events = [ev async for ev in runner.run(inv)]

    err = events[-1]
    assert err["type"] == "error"
    assert err["payload"]["category"] == "auth_terminal"
    assert err["payload"]["kind"] == "AuthTerminalError"
    # Codex was never spawned — the dead token short-circuits pre-spawn.
    assert fake.start_thread_calls == []
    refreshes = [e for e in elog.read() if e["type"] == "token_refresh"]
    assert refreshes[0]["payload"] == {"outcome": "failed_invalid_grant"}


@pytest.mark.anyio
async def test_fresh_token_proceeds_to_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.codex_fakes import (
        agent_message,
        mcp_call,
        thread_started,
        turn_completed,
    )

    _seed_auth(tmp_path)
    canonical = tmp_path / "auth.json"

    def _fresh(now_ms: int, *, env: object = None) -> ctr.RefreshOutcome:
        return ctr.RefreshOutcome(ctr.SKIPPED_FRESH)

    monkeypatch.setattr(codex_runner, "refresh_canonical_token", _fresh)

    scripted = [[
        thread_started("thr-1"),
        agent_message("ok"),
        mcp_call(name="complete_step", args={"answer": "42"},
                 result="done", item_id="cs-1"),
        turn_completed(input_tokens=1, output_tokens=1),
    ]]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted))
    runner._canonical_auth_override = canonical

    elog = InMemoryEventLog()
    inv = _build_invocation(tmp_path, event_log=elog)
    events = [ev async for ev in runner.run(inv)]

    kinds = [e.get("type") for e in events]
    assert "agent_result" in kinds, events           # spawn proceeded
    refreshes = [e for e in elog.read() if e["type"] == "token_refresh"]
    assert refreshes[0]["payload"] == {"outcome": "skipped_fresh"}
