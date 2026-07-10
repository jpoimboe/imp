"""AC 8: the pre-spawn token refresh fires ahead of both spawn dispositions and
journals one audit event per check."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, QuestContext, Message, TextBlock
from imp.agents.transcript import Transcript
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers import claude_code
from imp.providers import claude_token_refresh as ctr
from imp.providers.claude_code import ClaudeCodeAgentRunner
from tests.cc_fakes import FakeClaudeSDKClient, assistant, result, tool_use

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
            workflow_dir=tmp_path / ".imp" / "change" / "workflow"),
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

    def _fake_refresh(now_ms: int, *, env: Any = None) -> ctr.RefreshOutcome:
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

    def _fake_refresh(now_ms: int, *, env: Any = None) -> ctr.RefreshOutcome:
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
