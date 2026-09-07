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
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers import codex_runner
from imp.providers.codex_runner import CodexAgentRunner
from imp.providers.codex_token_refresh import SKIPPED_FRESH, RefreshOutcome
from tests.codex_fakes import FakeCodex, mcp_call, thread_started, turn_completed

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
) -> Ran:
    """Drive one codex turn against a chatgpt canonical credential with both
    refresh call sites stubbed, and return the outer event stream."""
    seed_canonical_chatgpt(tmp_path, monkeypatch)
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
