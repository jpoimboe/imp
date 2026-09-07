"""Scope 2: the codex auth-terminality probe — Rule A (it speaks only for the
credential this spawn runs on) and Rule B (imp's own marker identifies a dead
credential; the vendor's text is kept in `detail`)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from imp.agents.runner import AgentInvocation, QuestContext
from imp.engine import error_policy as ep
from imp.engine.executor import RunResult, run_quest
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers import codex_runner
from imp.providers import errors as perr
from imp.providers.codex_runner import CodexAgentRunner, CodexSession
from imp.providers.codex_token_refresh import (
    FAILED_INVALID_GRANT,
    FAILED_TRANSIENT,
    SKIPPED_FRESH,
    SKIPPED_ROTATED,
    RefreshOutcome,
)
from tests.codex_fakes import (
    FakeCodex,
    FakeTurnFailedEvent,
    mcp_call,
    thread_started,
    turn_completed,
)
from tests.test_executor_retry_continue import _SINGLE_WF, _WRITER, _make_quest
from tests.test_executor_rotation_race import _no_sleep, _retries, _ScriptProvider

_SRC = SourceLoc(file=Path("a.md"), line=1)

# A far-future JWT expiry: the value the spawn records and the probe must hand
# back as spawn_expires_at.
EXPIRY_SECONDS = 2_000_000_000
EXPIRY_MS = EXPIRY_SECONDS * 1000


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def seed_canonical_chatgpt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """An explicit-chatgpt canonical auth.json with a readable JWT expiry. Both
    `_canonical_auth_path()` and codex_token_refresh resolve $CODEX_HOME, so one
    setenv points the whole spawn at it."""
    home = tmp_path / "codex"
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {"access_token": _jwt(EXPIRY_SECONDS), "refresh_token": "r0"},
        "last_refresh": "2026-01-01T00:00:00Z",
    }), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return home / "auth.json"


class FakeRefresh:
    """Stands in for both refresh_canonical_token call sites and records how each
    was called: the pre-spawn one (force=False) and the probe (force=True)."""

    def __init__(self, *, pre: str, probe: str) -> None:
        self.calls: list[dict[str, Any]] = []
        self._pre = pre
        self._probe = probe

    def __call__(self, now_ms: int, *, env: Any = None, force: bool = False,
                 spawn_expires_at: int | None = None) -> RefreshOutcome:
        self.calls.append({"force": force, "spawn_expires_at": spawn_expires_at})
        return RefreshOutcome(self._probe if force else self._pre)


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t", description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="answer", type="string", required=True),),
            source=_SRC),
        prompt_template_source="", source_path=Path("a.md"), source=_SRC,
        tier=None)


def _invocation(tmp_path: Path, elog: InMemoryEventLog) -> AgentInvocation:
    (tmp_path / "tx").mkdir(exist_ok=True)
    return AgentInvocation(
        agent_def=_agent_def(), provider_name="codex", model_name="gpt-5.5",
        system_prompt="you are a test agent", initial_messages=(),
        quest_context=QuestContext(
            title="t", project_root=tmp_path, workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:t", event_log=elog)


def completing_turn() -> list[Any]:
    return [thread_started("thr-1"),
            mcp_call(name="complete_step", args={"answer": "42"},
                     result="step complete", item_id="cs-1"),
            turn_completed(input_tokens=1, output_tokens=1)]


# The codex server's own wording for a consumed refresh token.
REUSE_MESSAGE = (
    "Your access token could not be refreshed because your refresh token was "
    "already used. Please log out and sign in again.")


def failing_turn(error: Any) -> list[Any]:
    return [thread_started("thr-1"), FakeTurnFailedEvent(error=error)]


def error_payload(ran: Ran) -> dict[str, Any]:
    errs = [e["payload"] for e in ran.events if e.get("type") == "error"]
    assert len(errs) == 1, ran.events
    return errs[0]


class Ran(NamedTuple):
    events: list[dict[str, Any]]
    refresh: FakeRefresh
    runner: CodexAgentRunner
    elog: InMemoryEventLog


async def run_codex_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
    turn: list[Any],
    pre: str = SKIPPED_FRESH,
    probe: str = SKIPPED_FRESH,
    api_key: str | None = None,
) -> Ran:
    """Drive one codex turn against a chatgpt canonical credential with both
    refresh call sites stubbed, and return the outer event stream. `api_key`
    exports OPENAI_API_KEY after seeding, for the spawn-time fallback path."""
    seed_canonical_chatgpt(tmp_path, monkeypatch)
    if api_key is not None:
        monkeypatch.setenv("OPENAI_API_KEY", api_key)
    refresh = FakeRefresh(pre=pre, probe=probe)
    monkeypatch.setattr(codex_runner, "refresh_canonical_token", refresh)
    elog = InMemoryEventLog()
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=[turn]))
    events = [ev async for ev in runner.run(_invocation(tmp_path, elog))]
    return Ran(events, refresh, runner, elog)


def refresh_outcomes(ran: Ran) -> list[str]:
    return [e["payload"]["outcome"] for e in ran.elog.read()
            if e["type"] == "token_refresh"]


@pytest.mark.anyio
async def test_a_chatgpt_spawn_records_the_canonical_token_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read from disk after the pre-spawn refresh, so it is correct under every
    refresh outcome — and non-None, or rotation detection is dead in production."""
    ran = await run_codex_turn(tmp_path, monkeypatch, turn=completing_turn())

    assert [e.get("type") for e in ran.events].count("agent_result") == 1
    assert ran.runner._spawn_token_expiry == EXPIRY_MS


@pytest.mark.anyio
async def test_a_dead_grant_upgrades_the_turn_failure_to_auth_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule B: the emitted message is imp's own determination; the codex server's
    prose survives only under detail["provider_message"]."""
    ran = await run_codex_turn(
        tmp_path, monkeypatch, turn=failing_turn({"message": REUSE_MESSAGE}),
        probe=FAILED_INVALID_GRANT)
    payload = error_payload(ran)

    assert payload["category"] == "auth_terminal"
    assert payload["kind"] == "AuthTerminalError"
    assert payload["message"] == (
        "codex: ChatGPT OAuth refresh token is dead; run `codex login`. Sharing "
        "~/.codex/auth.json across machines kills it — each refresh rotates a "
        "single-use token.")
    assert payload["detail"]["provider_message"] == f"codex: {REUSE_MESSAGE}"
    # Pre-spawn refresh, then the probe — both journaled as token_refresh.
    assert refresh_outcomes(ran) == ["skipped_fresh", "failed_invalid_grant"]
    # No backoff is attempted for the emitted category: it pauses at once.
    assert ep.decide(payload["category"], prior_backoff_attempts=0) == ep.Pause()
    assert ep.budget_for(payload["category"]) is None


@pytest.mark.anyio
async def test_the_non_dict_error_shape_reaches_the_same_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An object whose str() carries the reuse message, rather than a dict."""

    class _OpaqueError:
        def __str__(self) -> str:
            return REUSE_MESSAGE

    ran = await run_codex_turn(
        tmp_path, monkeypatch, turn=failing_turn(_OpaqueError()),
        probe=FAILED_INVALID_GRANT)
    payload = error_payload(ran)

    assert payload["category"] == "auth_terminal"
    assert payload["detail"]["provider_message"] == f"codex: {REUSE_MESSAGE}"


@pytest.mark.anyio
async def test_a_transient_probe_leaves_the_401_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran = await run_codex_turn(
        tmp_path, monkeypatch,
        turn=failing_turn({"status": 401, "message": "Unauthorized"}),
        probe=FAILED_TRANSIENT)
    payload = error_payload(ran)

    assert payload["category"] == "auth"
    assert payload["kind"] == "AuthError"
    assert payload["message"] == "codex: Unauthorized"
    assert ep.is_retry_with_backoff(payload["category"]) is True


@pytest.mark.anyio
async def test_the_probe_hands_back_the_expiry_the_spawn_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran = await run_codex_turn(
        tmp_path, monkeypatch,
        turn=failing_turn({"status": 401, "message": "Unauthorized"}),
        probe=FAILED_TRANSIENT)

    assert ran.refresh.calls == [
        {"force": False, "spawn_expires_at": None},
        {"force": True, "spawn_expires_at": EXPIRY_MS},
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("auth_mode,err", [
    pytest.param("apikey", perr.AuthError("codex: Unauthorized"),
                 id="rule-a-apikey-spawn"),
    pytest.param("chatgpt", perr.NetworkError("codex: connection reset"),
                 id="not-an-auth-error"),
])
async def test_the_probe_stays_out_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    auth_mode: str, err: perr.ProviderError,
) -> None:
    """Rule A: after the later API-key fallback the canonical file still declares a
    dead chatgpt credential, so a probe that ignored the spawn's mode would pause
    the run over a credential it stopped using. The stub returns the dead-grant
    outcome for both call sites, so only the mode guard can keep this an
    AuthError — a stub returning NOT_APPLICABLE would pass without one."""
    seed_canonical_chatgpt(tmp_path, monkeypatch)
    refresh = FakeRefresh(pre=FAILED_INVALID_GRANT, probe=FAILED_INVALID_GRANT)
    monkeypatch.setattr(codex_runner, "refresh_canonical_token", refresh)
    elog = InMemoryEventLog()
    runner = CodexAgentRunner(model_name="gpt-5.5")
    runner._spawn_token_expiry = EXPIRY_MS
    session = CodexSession(_invocation(tmp_path, elog), runner)
    session.elog = elog
    session.aid = "step:t"
    session._proc_state = {"auth_mode": auth_mode}

    settled = await session._settle_auth_terminality(err)

    assert settled is err
    assert settled.category == err.category
    assert refresh.calls == []
    assert list(elog.read()) == []


@pytest.mark.anyio
async def test_a_peer_rotation_is_stamped_on_the_emitted_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asserted on the payload the arm yields, never on the exception: a payload
    that lost the stamp still disables the cheap retry while the object looks fine."""
    ran = await run_codex_turn(
        tmp_path, monkeypatch,
        turn=failing_turn({"status": 401, "message": "Unauthorized"}),
        probe=SKIPPED_ROTATED)
    payload = error_payload(ran)

    assert payload["category"] == "auth"
    assert payload["kind"] == "token_rotated"
    assert payload["message"] == "codex: Unauthorized"
    assert refresh_outcomes(ran) == ["skipped_fresh", "skipped_rotated"]


@pytest.mark.anyio
async def test_the_emitted_rotation_payload_takes_the_ladders_bottom_rung(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_registry: Any,
) -> None:
    """The executor is shared with Claude and untouched here; what this pins is
    that codex's own emitted payload reaches its `raced` branch."""
    ran = await run_codex_turn(
        tmp_path / "spawn", monkeypatch,
        turn=failing_turn({"status": 401, "message": "Unauthorized"}),
        probe=SKIPPED_ROTATED)
    payload = error_payload(ran)

    quest = _make_quest(tmp_path / "proj", agents={"writer.md": _WRITER},
                        workflow_yaml=_SINGLE_WF)
    provider = _ScriptProvider([payload])
    provider_registry("fake", env_check=lambda: True, factory=lambda _m: provider)
    monkeypatch.setattr("imp.engine.executor._interruptible_sleep", _no_sleep)

    events: list[dict[str, Any]] = []
    assert await run_quest(quest, on_event=events.append) is RunResult.COMPLETED

    retries = _retries(events)
    assert [p["backoff_seconds"] for p in retries] == [1.0]
    assert retries[0]["kind"] == "token_rotated"
