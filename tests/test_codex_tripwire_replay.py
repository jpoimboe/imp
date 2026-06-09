from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext
from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
from imp.providers.codex_runner import CodexAgentRunner
from tests.codex_fakes import (
    FakeCodex,
    agent_message,
    file_change,
    thread_started,
    turn_completed,
)

_SRC = SourceLoc(file=Path("a.md"), line=1)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _git_init(p: Path) -> None:
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.name", "T"], check=True)
    (p / "a.txt").write_text("ORIGINAL\n")
    subprocess.run(["git", "-C", str(p), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(p), "commit", "-m", "init", "-q"], check=True)


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t",
        description="",
        tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="answer", type="string", required=True),),
            source=_SRC,
        ),
        prompt_template_source="",
        source_path=Path("a.md"),
        source=_SRC,
        tier=None,
    )


def _build_invocation(tmp_path: Path) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(),
        provider_name="codex",
        model_name="gpt-5.5",
        system_prompt="sp",
        initial_messages=(),
        change_context=ChangeContext(
            slug="s", title="t", project_root=tmp_path,
            workflow_dir=tmp_path, id="c1",
        ),
        transcript_dir=tmp_path / "tx",
        fs_permissions=(
            FsRule(operations=("read", "write"), paths=("**",),
                   mode="allow", source=_SRC),
        ),
        events_path=tmp_path / "events.jsonl",
        agent_id="step:t",
    )


@pytest.mark.anyio
async def test_replay_with_file_change_fails_closed_and_reverts(
    tmp_path: Path,
) -> None:
    _git_init(tmp_path)
    (tmp_path / "auth.json").write_text('{"auth_mode": "chatgpt", "tokens": {}}')
    (tmp_path / "tx").mkdir(exist_ok=True)

    # Pre-existing untracked file that must survive the revert.
    pre_existing_untracked = tmp_path / "prior.txt"
    pre_existing_untracked.write_text("PRESERVE ME\n")

    scripted: list[list[Any]] = [[
        thread_started(),
        # Codex emits a file_change item — the native-edit tripwire must fire.
        file_change(str(tmp_path / "native-write.txt")),
        agent_message("did a native write — should never happen"),
        turn_completed(),
    ]]

    class _ScriptedThenWrite(FakeCodex):
        def start_thread(self, options: Any = None) -> Any:
            # Side-effect: write a stray file and corrupt the tracked file
            # BEFORE the stream begins, so the revert has state to clean up.
            (tmp_path / "native-write.txt").write_text("UNAUTH\n")
            (tmp_path / "a.txt").write_text("CORRUPTED\n")
            return super().start_thread(options)

    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: _ScriptedThenWrite(
            scripted_turns=scripted,
        ),
    )
    runner._canonical_auth_override = tmp_path / "auth.json"

    inv = _build_invocation(tmp_path)
    events = [ev async for ev in runner.run(inv)]

    # 1. Runner must yield an error event with kind == "native_tool_violation".
    err = next((e for e in events if e.get("type") == "error"), None)
    assert err is not None, f"no error event found; events={events}"
    assert err["payload"]["kind"] == "native_tool_violation"

    # 2. Tracked file must be restored to its committed state.
    assert (tmp_path / "a.txt").read_text() == "ORIGINAL\n"

    # 3. This-run-only untracked file must be removed by the revert.
    assert not (tmp_path / "native-write.txt").exists()

    # 4. Pre-existing untracked file must be preserved.
    assert pre_existing_untracked.exists()
    assert pre_existing_untracked.read_text() == "PRESERVE ME\n"
