"""The reported bug, encoded: a codex step entered on a worktree with an
unmerged index (`integrate-rebase.sh` exit 6 -> reconciler,
`autosquash-resolve-merge`) must start and run normally."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, QuestContext
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.codex_runner import CodexAgentRunner
from tests.codex_fakes import (
    FakeCodex,
    agent_message,
    mcp_call,
    thread_started,
    turn_completed,
)

_SRC = SourceLoc(file=Path("a.md"), line=1)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _git(repo: Path, *args: str,
         check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=check)


def _make_conflicted_rebase(repo: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "f.txt").write_text("feature\n")
    _git(repo, "commit", "-qam", "feature")
    _git(repo, "checkout", "-q", "main")
    (repo / "f.txt").write_text("main\n")
    _git(repo, "commit", "-qam", "main")
    _git(repo, "checkout", "-q", "feature")
    # `--merge` pins the merge backend (and so `.git/rebase-merge`) regardless of
    # an ambient `rebase.backend` setting. The conflict IS the setup, so this one
    # call is the only unchecked step.
    _git(repo, "rebase", "--merge", "main", check=False)


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t", description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="answer", type="string", required=True),),
            source=_SRC),
        prompt_template_source="", source_path=Path("a.md"), source=_SRC,
        tier=None)


def _build_invocation(tmp_path: Path) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(), provider_name="codex", model_name="gpt-5.5",
        system_prompt="sp", initial_messages=(),
        quest_context=QuestContext(title="t", project_root=tmp_path,
                                   workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:t")


@pytest.mark.anyio
async def test_session_completes_in_a_conflicted_mid_rebase_worktree(
    tmp_path: Path,
) -> None:
    _make_conflicted_rebase(tmp_path)
    (tmp_path / "auth.json").write_text('{"auth_mode": "chatgpt", "tokens": {}}')
    (tmp_path / "tx").mkdir(exist_ok=True)

    # Preconditions: this is the exact tree shape that broke the runner.
    assert _git(tmp_path, "status", "--porcelain").stdout.startswith("UU ")
    stash = _git(tmp_path, "stash", "create", check=False)
    assert stash.returncode != 0
    assert "Cannot save the current index state" in stash.stderr

    scripted = [[
        thread_started("thr-1"),
        agent_message("done"),
        mcp_call(name="complete_step", args={"answer": "42"},
                 result="ok", item_id="cs-1"),
        turn_completed(input_tokens=1, output_tokens=1),
    ]]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted))
    runner._canonical_auth_override = tmp_path / "auth.json"

    events = [ev async for ev in runner.run(_build_invocation(tmp_path))]
    out = next((e for e in events if e.get("type") == "agent_result"), None)
    assert out is not None, events
    assert out["payload"] == {"answer": "42"}

    # The run must leave the conflict exactly as it found it.
    stages = _git(tmp_path, "ls-files", "-u").stdout.splitlines()
    assert len(stages) == 3, stages
    assert (tmp_path / ".git" / "rebase-merge").is_dir()
