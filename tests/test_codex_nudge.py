# tests/test_codex_nudge.py
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
from imp.providers.codex_runner import CodexAgentRunner
from tests.codex_fakes import FakeCodex, agent_message, thread_started, turn_completed

_SRC = SourceLoc(file=Path("a.md"), line=1)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _seed(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    (tmp_path / ".gitkeep").write_text("")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".gitkeep"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "i"], check=True)
    auth = tmp_path / "auth.json"
    auth.write_text('{"auth_mode": "chatgpt", "tokens": {}}')
    (tmp_path / "tx").mkdir(exist_ok=True)
    return auth


def _inv(tmp_path, elog, *, continuation_fn) -> AgentInvocation:
    adef = AgentDef(
        name="t", description="", tools=frozenset({"complete_step"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="answer", type="string", required=True),),
            source=_SRC),
        prompt_template_source="", source_path=Path("a.md"), source=_SRC, tier=None)
    return AgentInvocation(
        agent_def=adef, provider_name="codex", model_name="gpt-5.5",
        system_prompt="sp", initial_messages=(),
        change_context=ChangeContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx",
        fs_permissions=(FsRule(operations=("read", "write"), paths=("**",),
                               mode="allow", source=_SRC),),
        events_path=tmp_path / "events.jsonl", agent_id="step:t",
        event_log=elog, continuation_fn=continuation_fn)


@pytest.mark.anyio
async def test_autonomous_run_nudges_then_terminates(tmp_path: Path) -> None:
    """#12: autonomous runs nudge via the continuation_fn until it returns None."""
    auth = _seed(tmp_path)
    elog = InMemoryEventLog()
    no_cs = [agent_message("incomplete"), turn_completed(output_tokens=1)]
    scripted = [[thread_started("t"), *no_cs]] + [list(no_cs) for _ in range(4)]

    calls = {"n": 0}
    def _nudge(_last: str):
        calls["n"] += 1
        return "please call complete_step" if calls["n"] < 2 else None  # self-limits

    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted))
    runner._canonical_auth_override = auth
    inv = _inv(tmp_path, elog, continuation_fn=_nudge)
    events = [ev async for ev in runner.run(inv)]

    assert calls["n"] >= 1  # autonomous DID nudge (was 0 before #12)
    assert events[-1]["payload"]["kind"] in (
        "no_complete_step_after_nudges", "agent_incomplete"), events[-1]
