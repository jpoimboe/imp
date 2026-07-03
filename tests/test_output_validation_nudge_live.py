"""Live nudge round-trips: a real agent deliberately malforms its first
complete_step, then re-emits correctly after the in-session nudge.

Gated on IMP_LIVE_PROVIDERS; skipped in `make check`. Run via `make test-live`
with the relevant provider opted in (and credentials configured).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.claude_code import ClaudeCodeAgentRunner
from imp.providers.codex_runner import CodexAgentRunner
from tests._live import assert_codex_toolchain, opted_in

_SRC = SourceLoc(file=Path("a.md"), line=1)

_MALFORM_THEN_FIX = (
    "Call the complete_step tool. IMPORTANT test protocol: on your FIRST call, "
    "intentionally OMIT the required field 'status' (send no fields at all). "
    "After you receive the validation error, call complete_step AGAIN with "
    "status set to \"Success\". Do not write anything else."
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _git_init(p: Path) -> None:
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.email", "t@t.t"],
                   check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.name", "T"], check=True)
    (p / "README.md").write_text("# t\n")
    subprocess.run(["git", "-C", str(p), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(p), "commit", "-m", "init", "-q"], check=True)


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t", description="", tools=frozenset({"complete_step"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=_SRC),
        prompt_template_source="", source_path=Path("a.md"), source=_SRC,
        tier=None)


def _inv(tmp_path: Path, provider: str, model: str,
         elog: InMemoryEventLog) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(), provider_name=provider, model_name=model,
        system_prompt=_MALFORM_THEN_FIX,
        initial_messages=(Message(role="user",
                                  content=(TextBlock(text="begin"),)),),
        change_context=ChangeContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx",
        events_path=tmp_path / "events.jsonl", agent_id="step:t", event_log=elog)


def _assert_recovered(events: list[dict], elog: InMemoryEventLog) -> None:
    kinds = [e.get("type") for e in events]
    assert "agent_output" in kinds, events
    assert "error" not in kinds, events
    log_types = [e["type"] for e in elog.read()]
    assert log_types.count("output_validation_nudge") >= 1, log_types


@pytest.mark.anyio
@pytest.mark.skipif(not opted_in("claude_code"),
                    reason="claude_code not in IMP_LIVE_PROVIDERS")
async def test_claude_code_live_nudge_round_trip(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "tx").mkdir(exist_ok=True)
    elog = InMemoryEventLog()
    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5")
    events = [e async for e in runner.run(
        _inv(tmp_path, "claude_code", "claude-sonnet-4-5", elog))]
    _assert_recovered(events, elog)


@pytest.mark.anyio
@pytest.mark.skipif(not opted_in("codex"),
                    reason="codex not in IMP_LIVE_PROVIDERS")
async def test_codex_live_nudge_round_trip(tmp_path: Path) -> None:
    assert_codex_toolchain()
    _git_init(tmp_path)
    (tmp_path / "tx").mkdir(exist_ok=True)
    elog = InMemoryEventLog()
    runner = CodexAgentRunner(model_name="gpt-5.5")
    events = [e async for e in runner.run(
        _inv(tmp_path, "codex", "gpt-5.5", elog))]
    _assert_recovered(events, elog)
