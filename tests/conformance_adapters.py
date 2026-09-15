"""Shared driver adapters for the provider conformance suite. Encapsulates
every per-provider divergence (fake wiring, uniform output schema, codex
git/auth seeding, initial-message shape) behind build/drive/run_scenario so
scenario tests carry only the pair-native fake script + assertions."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.paths import project_state_dir
from imp.providers.anthropic import AnthropicAgentRunner
from imp.providers.claude import ClaudeCodeAgentRunner
from imp.providers.codex_runner import CodexAgentRunner
from imp.providers.openai import OpenAIAgentRunner
from tests._events import control_events

PUSH = ("anthropic", "openai")
OBSERVE = ("claude_code", "codex")
PAIRS = PUSH + OBSERVE

_SRC = SourceLoc(file=Path("a.md"), line=1)
_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o-mini",
    "claude_code": "claude-sonnet-4-5",
    "codex": "gpt-5.5",
}


def agent_def(tools: frozenset[str] = frozenset({"complete_step"})) -> AgentDef:
    """Uniform status/note schema across all four pairs so complete_step args
    and the agent_result payload are identical (enables cross-Session parity)."""
    return AgentDef(
        name="t", description="", tools=tools, subagents=(),
        output_schema=OutputSchema(
            fields=(
                OutputField(name="status", type="enum",
                            values=("Success", "Error"), required=True),
                OutputField(name="note", type="string", required=False),
            ),
            source=_SRC),
        prompt_template_source="hi", source_path=Path("a.md"), source=_SRC)


def seed_codex(tmp_path: Path) -> Path:
    """Git repo + chatgpt auth.json + tx/ dir; returns the canonical auth path."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.t"],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"],
                   check=True)
    (tmp_path / ".gitkeep").write_text("")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".gitkeep"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"],
                   check=True)
    auth = tmp_path / "auth.json"
    auth.write_text('{"auth_mode": "chatgpt", "tokens": {}}')
    (tmp_path / "tx").mkdir(exist_ok=True)
    return auth


def build(pair: str, tmp_path: Path, scripted: Any, *,
          tools: frozenset[str] = frozenset({"complete_step"}),
          event_log: InMemoryEventLog | None = None,
          agent_id: str = "step:p/s", **inv_overrides: Any):
    """Return (runner, invocation) for `pair`, wiring the pair-native `scripted`
    fake payload into the right client_factory."""
    from tests.anthropic_fakes import FakeAsyncAnthropic
    from tests.cc_fakes import FakeClaudeSDKClient
    from tests.codex_fakes import FakeCodex
    from tests.openai_fakes import FakeResponsesClient

    (tmp_path / "art").mkdir(exist_ok=True)
    log = event_log if event_log is not None else InMemoryEventLog()

    if pair == "codex":
        workflow_dir, transcript_dir, init_msgs, qid = (
            tmp_path, tmp_path / "tx", (), "c1")
    else:
        workflow_dir = project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow"
        transcript_dir, qid = tmp_path / "step", "test"
        init_msgs = (Message(role="user", content=(TextBlock(text="go"),)),)

    inv_kwargs: dict[str, Any] = dict(
        agent_def=agent_def(tools),
        provider_name=pair, model_name=_MODELS[pair],
        system_prompt="be terse", initial_messages=init_msgs,
        quest_context=QuestContext(
            id=qid, title="Test", project_root=tmp_path, workflow_dir=workflow_dir),
        transcript_dir=transcript_dir, events_path=tmp_path / "events.jsonl",
        agent_id=agent_id, event_log=log)
    inv_kwargs.update(inv_overrides)
    inv = AgentInvocation(**inv_kwargs)

    if pair == "anthropic":
        runner: Any = AnthropicAgentRunner(
            model_name=_MODELS[pair],
            client_factory=lambda: FakeAsyncAnthropic(scripted))
    elif pair == "openai":
        runner = OpenAIAgentRunner(
            model_name=_MODELS[pair],
            client_factory=lambda: FakeResponsesClient(scripted))
    elif pair == "claude_code":
        runner = ClaudeCodeAgentRunner(
            model_name=_MODELS[pair],
            client_factory=lambda opts: FakeClaudeSDKClient(
                opts, scripted_messages=scripted))
    elif pair == "codex":
        auth = seed_codex(tmp_path)
        runner = CodexAgentRunner(
            model_name=_MODELS[pair],
            client_factory=lambda options=None: FakeCodex(scripted_turns=scripted))
        runner._canonical_auth_override = auth
    else:  # pragma: no cover
        raise ValueError(pair)
    return runner, inv


@dataclass
class Driven:
    control: list[dict[str, Any]]
    log: InMemoryEventLog
    raised: BaseException | None = None


async def drive(runner: Any, inv: AgentInvocation) -> Driven:
    """Run the runner to exhaustion, capturing yielded control events (minus
    heartbeats), the event log, and a parked AwaitingUserInput if one escapes."""
    yielded: list[dict[str, Any]] = []
    raised: BaseException | None = None
    try:
        async for ev in runner.run(inv):
            yielded.append(ev)
    except AwaitingUserInput as exc:
        raised = exc
    return Driven(control=control_events(yielded), log=inv.event_log, raised=raised)


async def run_scenario(pair: str, tmp_path: Path, scripted: Any, *,
                       tools: frozenset[str] = frozenset({"complete_step"}),
                       agent_id: str = "step:p/s", **inv_overrides: Any) -> Driven:
    runner, inv = build(pair, tmp_path, scripted, tools=tools,
                        agent_id=agent_id, **inv_overrides)
    return await drive(runner, inv)
