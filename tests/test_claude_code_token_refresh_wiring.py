"""AC 8: the pre-spawn token refresh fires ahead of both spawn dispositions and
journals one audit event per check."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
from imp.agents.transcript import Transcript
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.paths import project_state_dir
from imp.providers import claude_code
from imp.providers import claude_token_refresh as ctr
from imp.providers.claude_code import ClaudeCodeAgentRunner
from tests.cc_fakes import FakeClaudeSDKClient, assistant, result, text, tool_use

NOW = 1_000_000_000_000


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t", description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


def _inv(tmp_path: Path, log: InMemoryEventLog) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(),
        provider_name="claude_code", model_name="claude-sonnet-4-5",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow"),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=log,
    )


def _complete_step_client(opts: Any) -> FakeClaudeSDKClient:
    return FakeClaudeSDKClient(opts, scripted_messages=[
        assistant(tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ])


@pytest.mark.anyio
async def test_fresh_spawn_refreshes_before_config_dir_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "art").mkdir()
    order: list[str] = []

    def _fake_refresh(now_ms: int, *, env: Any = None,
                      force: bool = False) -> ctr.RefreshOutcome:
        order.append("refresh")
        return ctr.RefreshOutcome(ctr.REFRESHED, expires_at=NOW + 3600 * 1000)

    def _fake_materialize() -> Path:
        order.append("materialize")
        cfg = tmp_path / "cfg"
        (cfg / "projects").mkdir(parents=True, exist_ok=True)
        return cfg

    monkeypatch.setattr(claude_code, "refresh_canonical_token", _fake_refresh)
    monkeypatch.setattr(
        claude_code, "materialize_fresh_config_dir", _fake_materialize)

    log = InMemoryEventLog()
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=_complete_step_client)
    [ev async for ev in runner.run(_inv(tmp_path, log))]

    # Refresh runs BEFORE the fresh disposition copies canonical.
    assert order == ["refresh", "materialize"]
    events = [e for e in log.read() if e["type"] == "token_refresh"]
    assert len(events) == 1
    assert events[0]["agent_id"] == "step:p/s"
    # Exact-dict equality proves the payload carries ONLY outcome + expires_at —
    # no token material can appear.
    assert events[0]["payload"] == {
        "outcome": "refreshed", "expires_at": NOW + 3600 * 1000}


@pytest.mark.anyio
async def test_native_resume_refreshes_without_materializing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "art").mkdir()
    calls: list[str] = []

    def _fake_refresh(now_ms: int, *, env: Any = None,
                      force: bool = False) -> ctr.RefreshOutcome:
        calls.append("refresh")
        return ctr.RefreshOutcome(ctr.SKIPPED_FRESH)

    def _fake_materialize() -> Path:
        calls.append("materialize")
        raise AssertionError("native resume must not copy a fresh config dir")

    monkeypatch.setattr(claude_code, "refresh_canonical_token", _fake_refresh)
    monkeypatch.setattr(
        claude_code, "materialize_fresh_config_dir", _fake_materialize)

    log = InMemoryEventLog()
    inv = _inv(tmp_path, log)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)
    # Seed a resume-eligible transcript: assistant text tail + trailing plain-text
    # user turn drives the native-resume disposition (config_dir stays None).
    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(role="assistant", content=(TextBlock(text="hi"),)))
    t.append(Message(role="user", content=(TextBlock(text="keep going"),)))

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=_complete_step_client)
    [ev async for ev in runner.run(inv)]

    # Refresh fired; materialize was never reached (native path).
    assert calls == ["refresh"]
    events = [e for e in log.read() if e["type"] == "token_refresh"]
    assert len(events) == 1
    assert events[0]["payload"] == {"outcome": "skipped_fresh"}


@pytest.mark.anyio
async def test_dead_token_raises_terminal_auth_without_materializing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "art").mkdir()

    def _dead(now_ms: int, *, env: Any = None,
              force: bool = False) -> ctr.RefreshOutcome:
        return ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT)

    def _fake_materialize() -> Path:
        raise AssertionError("dead token must short-circuit before config copy")

    monkeypatch.setattr(claude_code, "refresh_canonical_token", _dead)
    monkeypatch.setattr(
        claude_code, "materialize_fresh_config_dir", _fake_materialize)

    log = InMemoryEventLog()
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=_complete_step_client)
    events = [ev async for ev in runner.run(_inv(tmp_path, log))]

    err = events[-1]
    assert err["type"] == "error"
    assert err["payload"]["category"] == "auth_terminal"
    assert err["payload"]["kind"] == "AuthTerminalError"
    # Config dir was never materialized — no claude -p spawn was built.
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert refreshes[0]["payload"] == {"outcome": "failed_invalid_grant"}


PROSE_401 = (
    "Failed to authenticate. API Error: 401 OAuth access token has been revoked.")


def _probe_seam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, forced_outcome: ctr.RefreshOutcome,
) -> list[bool]:
    """Seam: forced calls get forced_outcome, unforced get NOT_APPLICABLE; returns seen flags."""
    seen: list[bool] = []

    def _fake_refresh(now_ms: int, *, env: Any = None,
                      force: bool = False) -> ctr.RefreshOutcome:
        seen.append(force)
        return forced_outcome if force else ctr.RefreshOutcome(ctr.NOT_APPLICABLE)

    def _fake_materialize() -> Path:
        cfg = tmp_path / "cfg"
        (cfg / "projects").mkdir(parents=True, exist_ok=True)
        return cfg

    monkeypatch.setattr(claude_code, "refresh_canonical_token", _fake_refresh)
    monkeypatch.setattr(
        claude_code, "materialize_fresh_config_dir", _fake_materialize)
    return seen


def _run_scripted(tmp_path: Path, log: InMemoryEventLog,
                  scripted: list[Any]) -> Any:
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=lambda o: FakeClaudeSDKClient(o, scripted_messages=scripted))
    return runner.run(_inv(tmp_path, log))


@pytest.mark.anyio
async def test_dead_grant_probe_upgrades_the_auth_error_to_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "art").mkdir()
    seen = _probe_seam(
        monkeypatch, tmp_path, ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT))

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(
        tmp_path, log, [assistant(text(PROSE_401)), result()])]

    # Pre-spawn refresh unforced; the terminality probe forced.
    assert seen == [False, True]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth_terminal"]
    assert errs[0]["payload"]["kind"] == "AuthTerminalError"
    assert errs[0]["payload"]["message"] == (
        "claude: OAuth refresh token is dead; re-authentication required")
    # The upgrade inherits the classified error's detail verbatim.
    assert errs[0]["payload"]["detail"]["last_agent_text"] == PROSE_401
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in refreshes] == [
        {"outcome": "not_applicable"}, {"outcome": "failed_invalid_grant"}]


@pytest.mark.anyio
async def test_successful_probe_leaves_the_auth_error_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "art").mkdir()
    seen = _probe_seam(
        monkeypatch, tmp_path,
        ctr.RefreshOutcome(ctr.REFRESHED, expires_at=NOW + 3600 * 1000))

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(
        tmp_path, log, [assistant(text(PROSE_401)), result()])]

    assert seen == [False, True]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth"]
    assert errs[0]["payload"]["kind"] == "http_401"
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in refreshes] == [
        {"outcome": "not_applicable"},
        {"outcome": "refreshed", "expires_at": NOW + 3600 * 1000}]


@pytest.mark.anyio
async def test_probe_fires_on_the_termination_branch_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "art").mkdir()
    seen = _probe_seam(
        monkeypatch, tmp_path, ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT))

    log = InMemoryEventLog()
    # api_error_status labels the termination 'auth', exercising _termination_from_result.
    events = [ev async for ev in _run_scripted(tmp_path, log, [
        assistant(text("Session ended.")),
        result(is_error=True, api_error_status=401)])]

    assert seen == [False, True]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["auth_terminal"]
    assert errs[0]["payload"]["kind"] == "AuthTerminalError"
    # The merged termination diagnostics survive the type replacement.
    assert errs[0]["payload"]["detail"]["api_error_status"] == 401
    assert errs[0]["payload"]["detail"]["last_agent_text"] == "Session ended."
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in refreshes] == [
        {"outcome": "not_applicable"}, {"outcome": "failed_invalid_grant"}]


@pytest.mark.anyio
async def test_a_non_auth_error_triggers_no_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "art").mkdir()
    seen = _probe_seam(
        monkeypatch, tmp_path, ctr.RefreshOutcome(ctr.FAILED_INVALID_GRANT))

    log = InMemoryEventLog()
    events = [ev async for ev in _run_scripted(
        tmp_path, log, [assistant(text("API Error: 500 upstream is down")),
                        result()])]

    # Only the unforced pre-spawn refresh ran: no probe, no extra journal row.
    assert seen == [False]
    errs = [e for e in events if e["type"] == "error"]
    assert [e["payload"]["category"] for e in errs] == ["network"]
    refreshes = [e for e in log.read() if e["type"] == "token_refresh"]
    assert [e["payload"] for e in refreshes] == [{"outcome": "not_applicable"}]
