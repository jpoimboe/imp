# tests/test_codex_cancel_paths.py
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
from imp.providers.codex_runner import CodexAgentRunner
from tests.codex_fakes import FakeCodex

_SRC = SourceLoc(file=Path("a.md"), line=1)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _seed_git_and_auth(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    (tmp_path / ".gitkeep").write_text("")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".gitkeep"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True)
    auth = tmp_path / "auth.json"
    auth.write_text('{"auth_mode": "chatgpt", "tokens": {}}')
    (tmp_path / "tx").mkdir(exist_ok=True)
    return auth


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t", description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="answer", type="string", required=True),),
            source=_SRC),
        prompt_template_source="", source_path=Path("a.md"), source=_SRC, tier=None)


def _invocation(tmp_path: Path, *, event_log: object, interactive: bool = False,
                continuation_fn: object = None) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(), provider_name="codex", model_name="gpt-5.5",
        system_prompt="you are a test agent", initial_messages=(),
        change_context=ChangeContext(
            slug="s", title="t", project_root=tmp_path, workflow_dir=tmp_path, id="c1"),
        interactive=interactive, transcript_dir=tmp_path / "tx",
        fs_permissions=(FsRule(operations=("read", "write"), paths=("**",),
                               mode="allow", source=_SRC),),
        events_path=tmp_path / "events.jsonl", agent_id="step:t",
        event_log=event_log, continuation_fn=continuation_fn)


@pytest.mark.anyio
async def test_silent_stream_end_after_cancel_yields_interrupted(tmp_path: Path) -> None:
    auth = _seed_git_and_auth(tmp_path)
    elog = InMemoryEventLog()
    # An empty scripted turn: the event loop body never runs at all, so the
    # in-loop cancel check can't fire — the post-loop recheck must catch it.
    # on_turn_start fires before any events are yielded; with an empty event
    # list the inner `async for` body is never entered, so only the post-loop
    # recheck can produce the interrupted event.
    scripted = [[]]  # single empty turn — inner loop body never executes

    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(
            scripted_turns=scripted,
            on_turn_start=lambda: setattr(runner, "_cancel", True)))
    runner._canonical_auth_override = auth

    events = [ev async for ev in runner.run(_invocation(tmp_path, event_log=elog))]

    interrupted = [e for e in events if e.get("payload", {}).get("kind") == "interrupted"]
    assert interrupted, events
